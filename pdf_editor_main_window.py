import re
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import fitz  # PyMuPDF
from PySide6.QtCore import QEvent, QPoint, QSize, Qt, QTimer
from PySide6.QtGui import QAction, QBrush, QColor, QIcon, QImage, QKeySequence, QPen, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication, QDialog, QFileDialog, QGraphicsScene,
    QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidgetItem, QMainWindow,
    QListView, QListWidget, QMenu, QMessageBox, QPushButton, QSplitter, QTabWidget,
    QTextEdit, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from pdf_editor_models import (
    AnnotationEntry,
    CharInfo,
    LinkDeleteEntry,
    LinkEditEntry,
    NewLinkEntry,
    PageClipboard,
    SpanInfo,
    process_margin,
)
from pdf_text_edit_support import TextEditSupport
from pdf_editor_views import PdfView, ThumbnailListWidget


class MainWindow(QMainWindow):
    def _is_page_delete_shortcut_event(self, event) -> bool:
        try:
            key = event.key()
            mods = event.modifiers()
        except Exception:
            return False
        if key != Qt.Key_Backspace:
            return False
        return bool(mods & Qt.MetaModifier) or bool(mods & Qt.ControlModifier)

    def _is_text_input_widget(self, obj) -> bool:
        return isinstance(obj, (QLineEdit, QTextEdit))

    def eventFilter(self, obj, event):
        if event.type() == QEvent.KeyPress and self.isActiveWindow():
            try:
                if self._registration_mark_pick_mode and event.key() == Qt.Key_Escape:
                    self.cancel_registration_mark_pick_mode()
                    return True
            except Exception:
                pass
            if self._is_text_input_widget(obj):
                return False
            if self._has_open_doc():
                if self._is_page_delete_shortcut_event(event):
                    self.delete_selected_pages()
                    return True
        return super().eventFilter(obj, event)

    def keyPressEvent(self, event):
        if self._registration_mark_pick_mode and event.key() == Qt.Key_Escape:
            self.cancel_registration_mark_pick_mode()
            event.accept()
            return
        if self._has_open_doc():
            if self._is_page_delete_shortcut_event(event):
                self.delete_selected_pages()
                event.accept()
                return
        super().keyPressEvent(event)

    def _find_pending_new_link_at_point(self, page_idx: int, click_point: fitz.Point) -> Optional[int]:
        for i in range(len(self.new_links) - 1, -1, -1):
            nl = self.new_links[i]
            if nl.page_index != page_idx:
                continue
            try:
                r = fitz.Rect(nl.rect)
            except Exception:
                continue
            if (r + (-1, -1, 1, 1)).contains(click_point):
                return i
        return None

    def _cleanup_temp_file(self, path: Optional[Path]):
        if not path:
            return
        try:
            p = Path(path)
            if hasattr(self, "_session_temp_files"):
                self._session_temp_files.discard(p)
            if p.exists():
                p.unlink()
        except Exception:
            pass

    def _cleanup_obsolete_temp_files(self, keep: Optional[Set[Path]] = None):
        keep_set = {Path(p) for p in (keep or set()) if p}
        if not hasattr(self, "_session_temp_files"):
            return
        for p in list(self._session_temp_files):
            if p in keep_set:
                continue
            self._cleanup_temp_file(p)

    def _register_temp_file(self, path: Path):
        if not hasattr(self, "_session_temp_files"):
            self._session_temp_files = set()
        self._session_temp_files.add(Path(path))

    def _clear_page_clipboard(self):
        if self.page_clipboard:
            self._cleanup_temp_file(self.page_clipboard.pdf_path)
        self.page_clipboard = None
        self._update_page_action_ui()

    def _set_page_clipboard(self, clipboard: Optional[PageClipboard]):
        if self.page_clipboard and (clipboard is None or self.page_clipboard.pdf_path != clipboard.pdf_path):
            self._cleanup_temp_file(self.page_clipboard.pdf_path)
        self.page_clipboard = clipboard
        self._update_page_action_ui()

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Good PDF")
        self.resize(1150, 820)
        self.setAcceptDrops(True)
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

        self.doc: Optional[fitz.Document] = None
        self.original_path: Optional[Path] = None
        self.base_path: Optional[Path] = None
        self.temp_margin_file: Optional[Path] = None

        self.current_page_index = 0
        self.continuous_view = False

        self.zoom_levels = [0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.25, 2.5, 3.0, 3.5, 4.0, 5.0]
        self.zoom_index = self.zoom_levels.index(3.0)
        self.zoom = self.zoom_levels[self.zoom_index]

        self.scene = QGraphicsScene(self)
        self.view = PdfView(self)
        self.view.main_window = self
        self.view.setScene(self.scene)
        self.view.setAlignment(Qt.AlignLeft | Qt.AlignTop)

        self.current_spans: List[SpanInfo] = []
        self.current_spans_by_page: Dict[int, List[SpanInfo]] = {}
        self.link_edits: List[LinkEditEntry] = []
        self.new_links: List[NewLinkEntry] = []
        self.link_deletes: List[LinkDeleteEntry] = []
        self.page_clipboard: Optional[PageClipboard] = None

        self.undo_stack = []
        self.redo_stack = []
        self.max_undo_steps = 120

        self.modified = False
        self.save_path: Optional[Path] = None
        self.annotations: List[AnnotationEntry] = []
        self.annotation_mode: Optional[str] = None  # None, "highlight", "underline", "strikeout"

        self.search_query = ""
        self.search_results: List[Tuple[int, fitz.Rect]] = []
        self.search_index = -1
        self.last_auto_link_term = ""

        self._render_pix_cache = {}
        self._render_span_cache = {}
        self._render_cache_order = []
        self._render_cache_max = 24
        self._times_new_roman_paths_cache: Optional[List[Path]] = None
        self._unicode_font_paths_cache: Optional[List[Path]] = None
        self._system_font_files_cache: Optional[List[Path]] = None
        self._system_font_resources_cache = None
        self._fitz_font_cache = {}
        self._font_glyph_cache = {}
        self._session_temp_files: Set[Path] = set()
        self._page_words_cache: Dict[Tuple[str, int], list] = {}
        self._page_norm_words_cache: Dict[Tuple[str, int], List[str]] = {}
        self._page_word_first_index_cache: Dict[Tuple[str, int], Dict[str, List[int]]] = {}
        self._reference_rect_index: Dict[str, Dict[int, List[fitz.Rect]]] = {}
        self._reference_index_sig: Optional[Tuple[str, int]] = None
        self._page_base_spans_cache: Dict[Tuple[str, int], List[SpanInfo]] = {}
        self._page_scene_layouts: Dict[int, Tuple[float, float, float, float]] = {}
        self._thumbnail_doc_sig: Optional[int] = None
        self._thumbnail_cache: Dict[Tuple[str, int], QIcon] = {}
        self._thumbnail_selected_pages: Set[int] = {0}
        self._updating_thumbnail_list = False
        self._scroll_to_current_after_render = False
        self._scroll_thumbnail_to_current_after_render = False
        self._restore_thumbnail_scroll_value: Optional[int] = None
        self._visible_render_timer = QTimer(self)
        self._visible_render_timer.setSingleShot(True)
        self._visible_render_timer.timeout.connect(self._refresh_visible_after_scroll)
        self._scene_padding_x = 18.0
        self._scene_padding_y = 12.0
        self._scene_page_gap = 18.0
        self._page_render_oversample = 1.35
        self._thumbnail_render_oversample = 1.45
        self._pending_thumbnail_focus: Optional[Tuple[int, float, float]] = None
        self._pending_viewport_anchor: Optional[Tuple[int, float, float, float, float]] = None
        self._registration_mark_pick_mode = False
        self._thumbnail_icon_size = QSize(132, 176)
        self._thumbnail_item_size = QSize(152, 216)
        self._thumbnail_grid_size = QSize(154, 228)
        self._thumbnail_min_width = 188
        self._thumbnail_max_width = 242
        self.text_edit_support = TextEditSupport(self)

        self._init_ui()
        self._init_menu()
        self._bind_action_menus()
        self._init_shortcuts()
        self._setup_zoom_shortcuts()
        self._apply_ui_style()
        self._update_page_action_ui()

    # ---------------- UI ----------------

    def _init_ui(self):
        self.info_label = QLabel("")
        self.info_label.setObjectName("InfoLabel")

        self.show_links_btn = QPushButton("기존 링크 표시: OFF")
        self.show_links_btn.setCheckable(True)
        self.show_links_btn.toggled.connect(self.toggle_show_links)
        self.show_links_btn.setToolTip("기존 링크 강조 표시")

        self.auto_link_btn = QPushButton("자동 링크 추가")
        self.auto_link_btn.clicked.connect(self.auto_add_links)
        self.auto_link_btn.setToolTip("자동 링크 추가 / 삭제")

        self.prev_button = QPushButton("⬅️")
        self.next_button = QPushButton("➡️")
        self.prev_button.clicked.connect(self.prev_page)
        self.next_button.clicked.connect(self.next_page)
        self.prev_button.setFixedWidth(36)
        self.next_button.setFixedWidth(36)
        self.prev_button.setToolTip("이전 페이지 (←)")
        self.next_button.setToolTip("다음 페이지 (→)")

        self.page_input = QLineEdit()
        self.page_input.setFixedWidth(50)
        self.page_input.setAlignment(Qt.AlignCenter)
        self.page_input.setPlaceholderText("쪽")
        self.page_input.returnPressed.connect(self.go_to_page)
        self.page_input.setToolTip("페이지 번호 입력 후 Enter")

        self.total_page_label = QLabel("/ 0")

        nav = QHBoxLayout()
        nav.setContentsMargins(0, 0, 0, 0)
        nav.setSpacing(8)
        nav.addWidget(self.info_label)
        nav.addStretch(1)
        nav.addWidget(self.show_links_btn)
        nav.addWidget(self.auto_link_btn)
        nav.addWidget(QLabel(" | "))
        nav.addWidget(self.prev_button)
        nav.addWidget(self.page_input)
        nav.addWidget(self.total_page_label)
        nav.addWidget(self.next_button)

        self.view_toggle_btn = QPushButton("")
        self.view_toggle_btn.setMinimumWidth(132)
        self.view_toggle_btn.clicked.connect(self._toggle_view_mode_from_button)
        self._update_view_toggle_button()

        self.margin_btn = QPushButton("여백 조정")
        self.margin_btn.clicked.connect(self.open_margin_dialog)
        self.margin_btn.setToolTip("여백 / 크기 조정")

        self.highlight_btn = QPushButton("형광펜")
        self.highlight_btn.setCheckable(True)
        self.highlight_btn.setToolTip("텍스트 하이라이트 모드")
        self.highlight_btn.clicked.connect(lambda checked: self._set_annotation_mode("highlight" if checked else None))

        self.underline_btn = QPushButton("밑줄")
        self.underline_btn.setCheckable(True)
        self.underline_btn.setToolTip("텍스트 밑줄 모드")
        self.underline_btn.clicked.connect(lambda checked: self._set_annotation_mode("underline" if checked else None))

        self.strikeout_btn = QPushButton("취소선")
        self.strikeout_btn.setCheckable(True)
        self.strikeout_btn.setToolTip("텍스트 취소선 모드")
        self.strikeout_btn.clicked.connect(lambda checked: self._set_annotation_mode("strikeout" if checked else None))

        search_layout = QHBoxLayout()
        search_layout.setContentsMargins(0, 0, 0, 0)
        search_layout.setSpacing(8)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("텍스트 찾기 (Cmd+F)...")
        self.search_input.setFixedWidth(220)
        self.search_input.returnPressed.connect(self.next_search_result)
        self.search_input.textChanged.connect(self._on_search_text_changed)

        self.search_first_btn = QPushButton("⏮")
        self.search_first_btn.setFixedWidth(34)
        self.search_first_btn.clicked.connect(self.first_search_result)

        self.search_prev_btn = QPushButton("◀")
        self.search_prev_btn.setFixedWidth(34)
        self.search_prev_btn.clicked.connect(self.prev_search_result)

        self.search_next_btn = QPushButton("▶")
        self.search_next_btn.setFixedWidth(34)
        self.search_next_btn.clicked.connect(self.next_search_result)

        self.search_clear_btn = QPushButton("✕")
        self.search_clear_btn.setFixedWidth(34)
        self.search_clear_btn.clicked.connect(self.clear_search)
        self.search_clear_btn.setToolTip("검색 해제 (Esc)")

        self.search_label = QLabel("")
        self.search_label.setStyleSheet("color: #666666;")

        search_layout.addWidget(QLabel("🔍 검색:"))
        search_layout.addWidget(self.search_input)
        search_layout.addWidget(self.search_first_btn)
        search_layout.addWidget(self.search_prev_btn)
        search_layout.addWidget(self.search_next_btn)
        search_layout.addWidget(self.search_clear_btn)
        search_layout.addWidget(self.search_label)
        search_layout.addStretch(1)
        search_layout.addWidget(self.view_toggle_btn)
        search_layout.addWidget(self.margin_btn)
        search_layout.addWidget(self.highlight_btn)
        search_layout.addWidget(self.underline_btn)
        search_layout.addWidget(self.strikeout_btn)

        guide_text = (
            "<b>단어 클릭</b>: 텍스트 수정 &nbsp;|&nbsp; "
            "<b>Shift+클릭</b>: 줄 전체 수정 &nbsp;|&nbsp; "
            "<b>우클릭</b>: 링크 추가 &nbsp;|&nbsp; "
            "<b>Cmd+클릭</b>: 기존 링크 수정/제거 &nbsp;|&nbsp; "
            "<b>Cmd+우클릭</b>: 기존 링크 제거 &nbsp;|&nbsp; "
            "<b>방향키</b>: 페이지 이동 &nbsp;|&nbsp; "
            "<b>Cmd+=</b>: 확대 &nbsp;|&nbsp; <b>Cmd+-</b>: 축소"
        )
        self.guide_label = QLabel(guide_text)
        self.guide_label.setStyleSheet("color: #888888; font-size: 12px; margin-bottom: 2px;")
        self.guide_label.setAlignment(Qt.AlignCenter)

        self.thumbnail_list = ThumbnailListWidget(self)
        self.thumbnail_list.main_window = self
        self.thumbnail_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.thumbnail_list.setDragDropMode(QAbstractItemView.InternalMove)
        self.thumbnail_list.setDefaultDropAction(Qt.MoveAction)
        self.thumbnail_list.setIconSize(self._thumbnail_icon_size)
        self.thumbnail_list.setMinimumWidth(self._thumbnail_min_width)
        self.thumbnail_list.setMaximumWidth(self._thumbnail_max_width)
        self.thumbnail_list.setViewMode(QListWidget.IconMode)
        self.thumbnail_list.setMovement(QListWidget.Snap)
        self.thumbnail_list.setResizeMode(QListWidget.Adjust)
        self.thumbnail_list.setUniformItemSizes(True)
        self.thumbnail_list.setSpacing(9)
        self.thumbnail_list.setWordWrap(False)
        self.thumbnail_list.setGridSize(self._thumbnail_grid_size)
        self.thumbnail_list.setLayoutMode(QListView.Batched)
        self.thumbnail_list.setBatchSize(48)
        self.thumbnail_list.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.thumbnail_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.thumbnail_list.itemSelectionChanged.connect(self.on_thumbnail_selection_changed)
        self.thumbnail_list.verticalScrollBar().valueChanged.connect(self._on_thumbnail_scroll)

        self.sidebar_title = QLabel("페이지")
        self.sidebar_title.setObjectName("SidebarTitle")
        self.sidebar_hint = QLabel("클릭 위치로 이동, 다중 선택, 드래그 순서 변경, 우클릭 메뉴")
        self.sidebar_hint.setObjectName("SidebarHint")
        self.sidebar_selection_label = QLabel("선택 없음")
        self.sidebar_selection_label.setObjectName("SidebarSelectionLabel")

        # 페이지 탭
        page_tab = QWidget()
        page_tab_layout = QVBoxLayout(page_tab)
        page_tab_layout.setContentsMargins(4, 4, 4, 4)
        page_tab_layout.setSpacing(4)
        page_tab_layout.addWidget(self.sidebar_hint)
        page_tab_layout.addWidget(self.sidebar_selection_label)
        page_tab_layout.addWidget(self.thumbnail_list)

        # 북마크 탭
        self.bookmark_tree = QTreeWidget()
        self.bookmark_tree.setHeaderHidden(True)
        self.bookmark_tree.itemClicked.connect(self._on_bookmark_clicked)
        self.bookmark_tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.bookmark_tree.customContextMenuRequested.connect(self._on_bookmark_context_menu)

        bookmark_tab = QWidget()
        bookmark_tab_layout = QVBoxLayout(bookmark_tab)
        bookmark_tab_layout.setContentsMargins(4, 4, 4, 4)
        bookmark_tab_layout.addWidget(self.bookmark_tree)

        # 사이드바 탭 위젯
        self.sidebar_tabs = QTabWidget()
        self.sidebar_tabs.addTab(page_tab, "페이지")
        self.sidebar_tabs.addTab(bookmark_tab, "북마크")

        sidebar_widget = QWidget(self)
        sidebar_layout = QVBoxLayout(sidebar_widget)
        sidebar_layout.setContentsMargins(8, 6, 8, 6)
        sidebar_layout.setSpacing(4)
        sidebar_layout.addWidget(self.sidebar_tabs)

        self.main_splitter = QSplitter(Qt.Horizontal, self)
        self.main_splitter.addWidget(sidebar_widget)
        self.main_splitter.addWidget(self.view)
        self.main_splitter.setStretchFactor(0, 0)
        self.main_splitter.setStretchFactor(1, 1)
        self.main_splitter.setSizes([232, 918])
        self.view.verticalScrollBar().valueChanged.connect(self.queue_visible_refresh)
        self.view.horizontalScrollBar().valueChanged.connect(self.queue_visible_refresh)

        central = QWidget(self)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(8)
        layout.addLayout(nav)
        layout.addLayout(search_layout)
        layout.addWidget(self.main_splitter)
        self.setCentralWidget(central)
        self._update_search_action_ui()

    def _init_menu(self):
        menu = self.menuBar()
        file_menu = menu.addMenu("파일")
        file_menu.setToolTip("PDF 파일을 드래그하여 열기 | Cmd+O")
        edit_menu = menu.addMenu("편집")
        view_menu = menu.addMenu("보기")
        help_menu = menu.addMenu("도움말")

        self.open_action = QAction("PDF 열기", self)
        self.open_action.setShortcuts([QKeySequence("Ctrl+O"), QKeySequence("Meta+O")])
        self.open_action.triggered.connect(self.open_pdf)

        self.save_action = QAction("저장", self)
        self.save_action.setShortcuts([QKeySequence("Ctrl+S"), QKeySequence("Meta+S")])
        self.save_action.triggered.connect(self.save_pdf)

        self.save_as_action = QAction("다른 이름으로 저장...", self)
        self.save_as_action.setShortcuts([QKeySequence("Ctrl+Shift+S"), QKeySequence("Meta+Shift+S")])
        self.save_as_action.triggered.connect(self.save_pdf_as)

        self.undo_action = QAction("실행 취소", self)
        self.undo_action.setShortcuts([QKeySequence("Ctrl+Z"), QKeySequence("Meta+Z")])
        self.undo_action.triggered.connect(self.undo_last_action)

        self.redo_action = QAction("다시 실행", self)
        self.redo_action.setShortcuts([
            QKeySequence("Ctrl+Y"),
            QKeySequence("Ctrl+Shift+Z"),
            QKeySequence("Meta+Shift+Z"),
        ])
        self.redo_action.triggered.connect(self.redo_last_action)

        self.cut_page_action = QAction("페이지 잘라내기", self)
        self.cut_page_action.setShortcuts([QKeySequence("Ctrl+Shift+X"), QKeySequence("Meta+Shift+X")])
        self.cut_page_action.triggered.connect(self.cut_selected_pages)

        self.copy_page_action = QAction("페이지 복사", self)
        self.copy_page_action.setShortcuts([QKeySequence("Ctrl+Shift+C"), QKeySequence("Meta+Shift+C")])
        self.copy_page_action.triggered.connect(self.copy_selected_pages)

        self.paste_page_action = QAction("페이지 붙여넣기", self)
        self.paste_page_action.setShortcuts([QKeySequence("Ctrl+Shift+V"), QKeySequence("Meta+Shift+V")])
        self.paste_page_action.triggered.connect(self.paste_pages_after_selection)

        self.duplicate_page_action = QAction("페이지 복제", self)
        self.duplicate_page_action.setShortcuts([QKeySequence("Ctrl+Shift+D"), QKeySequence("Meta+Shift+D")])
        self.duplicate_page_action.triggered.connect(self.duplicate_selected_pages)

        self.rotate_page_action = QAction("페이지 90도 회전", self)
        self.rotate_page_action.setShortcuts([QKeySequence("Ctrl+Shift+R"), QKeySequence("Meta+Shift+R")])
        self.rotate_page_action.triggered.connect(self.rotate_selected_pages_clockwise)

        self.insert_blank_page_action = QAction("빈 페이지 삽입", self)
        self.insert_blank_page_action.triggered.connect(self.insert_blank_page)

        self.insert_from_pdf_action = QAction("다른 PDF에서 페이지 삽입...", self)
        self.insert_from_pdf_action.triggered.connect(self.insert_pages_from_pdf)

        self.margin_action = QAction("여백 / 크기 조정...", self)
        self.margin_action.triggered.connect(self.open_margin_dialog)

        self.remove_registration_marks_pick_action = QAction("인쇄 마크 제거 (클릭 지정)...", self)
        self.remove_registration_marks_pick_action.triggered.connect(self.begin_registration_mark_pick_mode)

        self.export_image_action = QAction("페이지를 이미지로 내보내기...", self)
        self.export_image_action.triggered.connect(self.export_pages_as_images)

        self.delete_page_action = QAction("페이지 삭제", self)
        self.delete_page_action.setShortcuts([QKeySequence("Meta+Backspace"), QKeySequence("Ctrl+Backspace")])
        self.delete_page_action.triggered.connect(self.delete_selected_pages)

        self.continuous_view_action = QAction("연속 페이지 보기", self)
        self.continuous_view_action.setCheckable(True)
        self.continuous_view_action.setShortcuts([QKeySequence("Ctrl+Shift+P"), QKeySequence("Meta+Shift+P")])
        self.continuous_view_action.toggled.connect(self.toggle_continuous_view)

        file_menu.addAction(self.open_action)
        file_menu.addAction(self.save_action)
        file_menu.addAction(self.save_as_action)

        edit_menu.addAction(self.undo_action)
        edit_menu.addAction(self.redo_action)
        edit_menu.addSeparator()
        edit_menu.addAction(self.cut_page_action)
        edit_menu.addAction(self.copy_page_action)
        edit_menu.addAction(self.paste_page_action)
        edit_menu.addAction(self.duplicate_page_action)
        edit_menu.addAction(self.rotate_page_action)
        edit_menu.addSeparator()
        edit_menu.addAction(self.insert_blank_page_action)
        edit_menu.addAction(self.insert_from_pdf_action)
        edit_menu.addSeparator()
        edit_menu.addAction(self.delete_page_action)
        edit_menu.addAction(self.margin_action)
        edit_menu.addAction(self.remove_registration_marks_pick_action)

        file_menu.addSeparator()
        file_menu.addAction(self.export_image_action)

        view_menu.addAction(self.continuous_view_action)

        help_action = QAction("단축키 / 사용법", self)
        help_action.triggered.connect(self.show_help_dialog)
        help_menu.addAction(help_action)

        formula_help_action = QAction("수식 도움말", self)
        formula_help_action.triggered.connect(self.show_formula_help_dialog)
        help_menu.addAction(formula_help_action)

        developer_action = QAction("개발자 정보", self)
        developer_action.triggered.connect(self.show_developer_info_dialog)
        help_menu.addAction(developer_action)

        if hasattr(self, "statusBar") and callable(self.statusBar):
            self.statusBar().showMessage("PDF 파일을 드래그하여 열기", 3000)

    def _init_shortcuts(self):
        QShortcut(QKeySequence(Qt.Key_Left), self).activated.connect(self.prev_page)
        QShortcut(QKeySequence(Qt.Key_Right), self).activated.connect(self.next_page)
        QShortcut(QKeySequence("Ctrl+F"), self).activated.connect(self.focus_search)
        QShortcut(QKeySequence("Meta+F"), self).activated.connect(self.focus_search)
        QShortcut(QKeySequence(Qt.Key_Escape), self.search_input, activated=self.clear_search)
        QShortcut(QKeySequence("Ctrl+Shift+L"), self).activated.connect(self.auto_add_links)
        QShortcut(QKeySequence("Meta+Shift+L"), self).activated.connect(self.auto_add_links)
        QShortcut(QKeySequence("Ctrl+W"), self, activated=self.close)
        QShortcut(QKeySequence("Meta+W"), self, activated=self.close)

    def _setup_zoom_shortcuts(self):
        QShortcut(QKeySequence("Ctrl+="), self, activated=self.zoom_in)
        QShortcut(QKeySequence("Ctrl+-"), self, activated=self.zoom_out)
        QShortcut(QKeySequence("Meta+="), self, activated=self.zoom_in)
        QShortcut(QKeySequence("Meta+-"), self, activated=self.zoom_out)

    def _bind_action_menus(self):
        self.delete_page_action.setToolTip("선택 페이지 삭제 (Cmd+Backspace)")
        self.cut_page_action.setToolTip("선택 페이지 잘라내기 (Cmd+Shift+X)")
        self.copy_page_action.setToolTip("선택 페이지 복사 (Cmd+Shift+C)")
        self.paste_page_action.setToolTip("선택 페이지 붙여넣기 (Cmd+Shift+V)")
        self.duplicate_page_action.setToolTip("선택 페이지 복제 (Cmd+Shift+D)")
        self.rotate_page_action.setToolTip("선택 페이지 시계 방향 90도 회전 (Cmd+Shift+R)")
        self.margin_action.setToolTip("여백 / 크기 조정")
        self.remove_registration_marks_pick_action.setToolTip("지울 마크를 한 번 클릭해서 모든 페이지 같은 위치에서 제거")
        self.continuous_view_action.setToolTip("연속 페이지 보기 전환 (Cmd+Shift+P)")

        page_menu = QMenu(self)
        page_menu.addAction(self.cut_page_action)
        page_menu.addAction(self.copy_page_action)
        page_menu.addAction(self.paste_page_action)
        page_menu.addAction(self.duplicate_page_action)
        page_menu.addAction(self.rotate_page_action)
        page_menu.addSeparator()
        page_menu.addAction(self.delete_page_action)
        for action in (
            self.cut_page_action,
            self.copy_page_action,
            self.paste_page_action,
            self.duplicate_page_action,
            self.rotate_page_action,
            self.delete_page_action,
        ):
            self.thumbnail_list.addAction(action)
        self.thumbnail_list.setContextMenuPolicy(Qt.ActionsContextMenu)

    def _format_page_selection_summary(self, page_indices: List[int]) -> str:
        if not page_indices:
            return "선택 없음"
        if len(page_indices) == 1:
            return f"{page_indices[0] + 1}쪽 선택"

        parts = []
        start = page_indices[0]
        prev = start
        for idx in page_indices[1:]:
            if idx == prev + 1:
                prev = idx
                continue
            parts.append(f"{start + 1}" if start == prev else f"{start + 1}-{prev + 1}")
            start = prev = idx
        parts.append(f"{start + 1}" if start == prev else f"{start + 1}-{prev + 1}")
        ranges = ", ".join(parts[:3])
        if len(parts) > 3:
            ranges += ", ..."
        return f"{len(page_indices)}개 선택 · {ranges}"

    def _view_toggle_button_text(self) -> str:
        return "단일 보기로 전환" if self.continuous_view else "연속 보기로 전환"

    def _view_toggle_button_tooltip(self) -> str:
        if self.continuous_view:
            return "현재 연속 보기입니다. 클릭하면 단일 보기로 바뀝니다."
        return "현재 단일 보기입니다. 클릭하면 연속 보기로 바뀝니다."

    def _update_view_toggle_button(self):
        if not hasattr(self, "view_toggle_btn"):
            return
        self.view_toggle_btn.setText(self._view_toggle_button_text())
        self.view_toggle_btn.setToolTip(self._view_toggle_button_tooltip())

    def _toggle_view_mode_from_button(self, _checked: bool = False):
        self.toggle_continuous_view(not self.continuous_view)

    def _update_registration_mark_pick_cursor(self):
        if not hasattr(self, "view"):
            return
        if self._registration_mark_pick_mode:
            self.view.setCursor(Qt.CrossCursor)
            self.view.viewport().setCursor(Qt.CrossCursor)
        else:
            self.view.unsetCursor()
            self.view.viewport().unsetCursor()

    def begin_registration_mark_pick_mode(self):
        if not self._has_open_doc():
            QMessageBox.warning(self, "알림", "먼저 PDF 파일을 열어주세요.")
            return
        self._registration_mark_pick_mode = True
        self._update_registration_mark_pick_cursor()
        self.statusBar().showMessage(
            "지울 인쇄 마크를 PDF 위에서 한 번 클릭하세요. Esc로 취소할 수 있습니다.",
            8000,
        )

    def cancel_registration_mark_pick_mode(self, show_message: bool = True):
        if not self._registration_mark_pick_mode:
            return
        self._registration_mark_pick_mode = False
        self._update_registration_mark_pick_cursor()
        if show_message:
            self.statusBar().showMessage("인쇄 마크 클릭 지정이 취소되었습니다.", 3000)

    def _handle_registration_mark_pick(self, click_point: fitz.Point, page_index: int) -> bool:
        if not self._registration_mark_pick_mode:
            return False
        self._registration_mark_pick_mode = False
        self._update_registration_mark_pick_cursor()
        self.remove_registration_marks_at_point(click_point, page_index)
        return True

    def _update_page_action_ui(self):
        has_doc = self._has_open_doc()
        selected_pages = self._selected_page_indices() if has_doc else []
        count = len(selected_pages)
        can_delete = has_doc and count > 0 and count < len(self.doc)
        can_operate = has_doc and count > 0
        can_paste = has_doc and self.page_clipboard is not None

        self.delete_page_action.setEnabled(can_delete)
        self.cut_page_action.setEnabled(can_delete)
        self.copy_page_action.setEnabled(can_operate)
        self.duplicate_page_action.setEnabled(can_operate)
        self.rotate_page_action.setEnabled(can_operate)
        self.paste_page_action.setEnabled(can_paste)
        self.margin_action.setEnabled(has_doc)
        self.remove_registration_marks_pick_action.setEnabled(has_doc)

        self.view_toggle_btn.setEnabled(has_doc)
        self.margin_btn.setEnabled(has_doc)
        self._update_view_toggle_button()
        if hasattr(self, "sidebar_selection_label"):
            self.sidebar_selection_label.setText(self._format_page_selection_summary(selected_pages))

    def _apply_ui_style(self):
        self.setStyleSheet(
            """
            QMainWindow, QWidget {
                background: #17181c;
                color: #f2f3f5;
            }
            QLabel#InfoLabel {
                color: #f5f6f7;
                font-size: 13px;
                font-weight: 600;
            }
            QLabel#SidebarTitle {
                font-size: 13px;
                font-weight: 700;
                color: #f5f6f7;
            }
            QLabel#SidebarHint {
                color: #8f98a3;
                font-size: 11px;
            }
            QLabel#SidebarSelectionLabel {
                color: #c5d1e0;
                font-size: 12px;
                font-weight: 600;
            }
            QPushButton, QToolButton {
                background: #23262d;
                border: 1px solid #323743;
                border-radius: 8px;
                padding: 5px 10px;
                min-height: 26px;
            }
            QPushButton:hover, QToolButton:hover {
                background: #2d323c;
            }
            QPushButton:pressed, QToolButton:pressed {
                background: #1d2128;
                border-color: #5a6b84;
            }
            QPushButton:checked, QToolButton:checked {
                background: #30466b;
                border-color: #476aa3;
            }
            QToolButton::menu-indicator {
                width: 0px;
                image: none;
            }
            QLineEdit {
                background: #111318;
                border: 1px solid #323743;
                border-radius: 8px;
                padding: 5px 8px;
                min-height: 26px;
            }
            QListWidget {
                background: #111318;
                border: 1px solid #2a2f39;
                border-radius: 10px;
                padding: 6px;
            }
            QListWidget::item {
                border-radius: 8px;
                padding: 6px;
                margin: 2px;
            }
            QListWidget::item:selected {
                background: #2a3b57;
                border: 1px solid #476aa3;
            }
            QSplitter::handle {
                background: #1f2229;
                width: 1px;
            }
            """
        )

    # ---------------- Utils ----------------

    def _has_open_doc(self) -> bool:
        return self.doc is not None and self.base_path is not None and self.original_path is not None

    def _mark_modified(self):
        self.modified = True

    def _clear_search_state(self):
        self.search_query = ""
        self.search_results.clear()
        self.search_index = -1
        self.search_input.clear()
        self.search_label.setText("")
        self._update_search_action_ui()

    def _current_search_text(self) -> str:
        return self.search_input.text().strip()

    def _clear_search_matches(self, rerender: bool = False):
        self.search_results.clear()
        self.search_index = -1
        self.search_label.setText("")
        self._update_search_action_ui()
        if rerender:
            self.render_page()

    def _on_search_text_changed(self, _text: str):
        query = self._current_search_text()
        if not query:
            if self.search_query or self.search_results or self.search_index != -1:
                self.search_query = ""
                self._clear_search_matches(rerender=True)
            else:
                self._update_search_action_ui()
            return

        if query != self.search_query:
            had_visible_matches = bool(self.search_results)
            self.search_results.clear()
            self.search_index = -1
            self.search_label.setText("")
            self._update_search_action_ui()
            if had_visible_matches:
                self.render_page()
            return

        self._update_search_action_ui()

    def _update_search_action_ui(self):
        has_query = bool(self._current_search_text())
        has_results = bool(self.search_results)
        self.search_clear_btn.setEnabled(has_query or has_results)
        self.search_first_btn.setEnabled(has_query or has_results)
        self.search_prev_btn.setEnabled(has_query or has_results)
        self.search_next_btn.setEnabled(has_query or has_results)

    def _push_undo_snapshot(self):
        snapshot = (
            [LinkEditEntry(**vars(e)) for e in self.link_edits],
            [NewLinkEntry(**vars(e)) for e in self.new_links],
            [LinkDeleteEntry(**vars(e)) for e in self.link_deletes],
            self.base_path,
            self.temp_margin_file,
            self.current_page_index,
        )
        self.undo_stack.append(snapshot)
        self.redo_stack.clear()
        if len(self.undo_stack) > self.max_undo_steps:
            self.undo_stack.pop(0)

    def _restore_snapshot(self, snapshot):
        prev_links, prev_new_links, prev_deletes, prev_base_path, prev_temp_margin, prev_page_index = snapshot
        self.link_edits = prev_links
        self.new_links = prev_new_links
        self.link_deletes = prev_deletes
        self.base_path = prev_base_path
        self.temp_margin_file = prev_temp_margin

        if self.base_path:
            try:
                self.doc = fitz.open(str(self.base_path))
                self.current_page_index = min(prev_page_index, len(self.doc) - 1)
                self._invalidate_render_cache()
                self._thumbnail_selected_pages = {self.current_page_index}
                self._invalidate_thumbnail_cache()
            except Exception:
                self.doc = None
        else:
            self.current_page_index = 0
        self._update_page_action_ui()

    def _remap_page_index_after_delete_set(self, page_index: int, deleted_page_indices: List[int]) -> Optional[int]:
        deleted_set = set(deleted_page_indices)
        if page_index in deleted_set:
            return None
        shift = sum(1 for idx in deleted_page_indices if idx < page_index)
        return page_index - shift

    def _contiguous_ranges(self, page_indices: List[int]) -> List[Tuple[int, int]]:
        ordered = sorted(set(page_indices))
        if not ordered:
            return []
        ranges: List[Tuple[int, int]] = []
        start = ordered[0]
        prev = ordered[0]
        for idx in ordered[1:]:
            if idx == prev + 1:
                prev = idx
                continue
            ranges.append((start, prev))
            start = prev = idx
        ranges.append((start, prev))
        return ranges

    def _rebuild_pending_changes_with_mapping(self, page_mapper, target_mapper):
        rebuilt_link_edits: List[LinkEditEntry] = []
        for e in self.link_edits:
            new_page_index = page_mapper(e.page_index)
            if new_page_index is None:
                continue
            new_target_page = target_mapper(e.new_page)
            if new_target_page is None:
                continue
            rebuilt_link_edits.append(
                LinkEditEntry(
                    page_index=new_page_index,
                    link_rect=e.link_rect,
                    new_page=new_target_page,
                )
            )
        self.link_edits = rebuilt_link_edits

        rebuilt_new_links: List[NewLinkEntry] = []
        for e in self.new_links:
            new_page_index = page_mapper(e.page_index)
            if new_page_index is None:
                continue
            new_target_page = target_mapper(e.target_page)
            if new_target_page is None:
                continue
            rebuilt_new_links.append(
                NewLinkEntry(
                    page_index=new_page_index,
                    rect=e.rect,
                    target_page=new_target_page,
                )
            )
        self.new_links = rebuilt_new_links

        rebuilt_link_deletes: List[LinkDeleteEntry] = []
        for e in self.link_deletes:
            new_page_index = page_mapper(e.page_index)
            if new_page_index is None:
                continue
            rebuilt_link_deletes.append(
                LinkDeleteEntry(
                    page_index=new_page_index,
                    link_rect=e.link_rect,
                )
            )
        self.link_deletes = rebuilt_link_deletes

    def _rebuild_pending_changes_after_page_delete_many(self, deleted_page_indices: List[int]):
        ordered = sorted(set(deleted_page_indices))
        self._rebuild_pending_changes_with_mapping(
            lambda idx: self._remap_page_index_after_delete_set(idx, ordered),
            lambda idx: self._remap_page_index_after_delete_set(idx, ordered),
        )

    def _rebuild_pending_changes_after_insert(self, insert_at: int, page_count: int):
        self._rebuild_pending_changes_with_mapping(
            lambda idx: idx + page_count if idx >= insert_at else idx,
            lambda idx: idx + page_count if idx >= insert_at else idx,
        )

    def _rebuild_pending_changes_after_reorder(self, old_to_new: Dict[int, int]):
        self._rebuild_pending_changes_with_mapping(
            lambda idx: old_to_new.get(idx),
            lambda idx: old_to_new.get(idx),
        )

    def _invalidate_thumbnail_cache(self, structure_changed: bool = True):
        if structure_changed:
            self._thumbnail_doc_sig = None
        self._thumbnail_cache.clear()

    def _thumbnail_cache_key(self, page_idx: int) -> Tuple[str, int]:
        icon_size = self.thumbnail_list.iconSize()
        return (
            str(self.base_path),
            int(page_idx),
            int(icon_size.width()),
            int(icon_size.height()),
            round(float(self._thumbnail_render_oversample), 2),
        )

    def _thumbnail_icon_for_page(self, page_idx: int) -> QIcon:
        key = self._thumbnail_cache_key(page_idx)
        icon = self._thumbnail_cache.get(key)
        if icon is not None:
            return icon

        page = self.doc[page_idx]
        page_rect = page.rect
        icon_size = self.thumbnail_list.iconSize()
        scale = min(
            float(icon_size.width()) / max(1.0, float(page_rect.width)),
            float(icon_size.height()) / max(1.0, float(page_rect.height)),
        )
        render_scale = max(0.05, scale * float(self._thumbnail_render_oversample))
        pix = page.get_pixmap(matrix=fitz.Matrix(render_scale, render_scale), alpha=False)
        image = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format_RGB888)
        pixmap = QPixmap.fromImage(image)
        icon = QIcon(pixmap)
        self._thumbnail_cache[key] = icon
        return icon

    def _thumbnail_image_rect(self, item: QListWidgetItem) -> Optional[Tuple[float, float, float, float]]:
        if not self.doc or item is None:
            return None
        page_idx = item.data(Qt.UserRole)
        if not isinstance(page_idx, int) or page_idx < 0 or page_idx >= len(self.doc):
            return None

        item_rect = self.thumbnail_list.visualItemRect(item)
        if item_rect.isNull():
            return None

        page_rect = self.doc[page_idx].rect
        icon_size = self.thumbnail_list.iconSize()
        scale = min(
            float(icon_size.width()) / max(1.0, float(page_rect.width)),
            float(icon_size.height()) / max(1.0, float(page_rect.height)),
        )
        image_w = max(1.0, float(page_rect.width) * scale)
        image_h = max(1.0, float(page_rect.height) * scale)

        label_height = self.thumbnail_list.fontMetrics().height() + 18
        content_left = float(item_rect.left() + 8)
        content_top = float(item_rect.top() + 6)
        content_width = max(image_w, float(item_rect.width() - 16))
        content_height = max(image_h, float(item_rect.height() - label_height - 10))
        image_x = content_left + max(0.0, (content_width - image_w) * 0.5)
        image_y = content_top + max(0.0, (content_height - image_h) * 0.5)
        return (image_x, image_y, image_w, image_h)

    def _thumbnail_focus_fraction_from_click(
        self,
        item: QListWidgetItem,
        click_pos,
    ) -> Tuple[float, float]:
        image_rect = self._thumbnail_image_rect(item)
        if image_rect is None:
            return (0.5, 0.5)

        x0, y0, width, height = image_rect
        px = float(click_pos.x())
        py = float(click_pos.y())
        if not (x0 <= px <= x0 + width and y0 <= py <= y0 + height):
            return (0.5, 0.5)

        fx = (px - x0) / max(1.0, width)
        fy = (py - y0) / max(1.0, height)
        return (
            max(0.0, min(1.0, fx)),
            max(0.0, min(1.0, fy)),
        )

    def focus_thumbnail_click(self, item: QListWidgetItem, click_pos):
        if not self.doc or item is None:
            return
        page_idx = item.data(Qt.UserRole)
        if not isinstance(page_idx, int) or page_idx < 0 or page_idx >= len(self.doc):
            return

        fx, fy = self._thumbnail_focus_fraction_from_click(item, click_pos)
        self._pending_thumbnail_focus = (page_idx, fx, fy)

        if self.current_page_index != page_idx:
            self.current_page_index = page_idx
            self._thumbnail_selected_pages = {page_idx}
            self._sync_thumbnail_selection()
            self.render_page()
            return

        if not self._apply_pending_thumbnail_focus():
            self._scroll_to_current_after_render = True
            self.render_page()

    def _visible_thumbnail_page_candidates(self) -> Set[int]:
        candidates = set()
        if not self.doc:
            return candidates

        base = self.current_page_index
        for idx in range(max(0, base - 8), min(len(self.doc), base + 9)):
            candidates.add(idx)

        top_item = self.thumbnail_list.itemAt(8, 8)
        bottom_item = self.thumbnail_list.itemAt(8, max(8, self.thumbnail_list.viewport().height() - 8))
        top_row = top_item.data(Qt.UserRole) if top_item else max(0, base - 6)
        bottom_row = bottom_item.data(Qt.UserRole) if bottom_item else min(len(self.doc) - 1, base + 6)
        for idx in range(max(0, top_row - 4), min(len(self.doc), bottom_row + 5)):
            candidates.add(idx)

        for idx in self._thumbnail_selected_pages:
            candidates.add(idx)
        return candidates

    def _ensure_thumbnail_icons(self):
        if not self.doc:
            return
        for page_idx in sorted(self._visible_thumbnail_page_candidates()):
            if 0 <= page_idx < self.thumbnail_list.count():
                item = self.thumbnail_list.item(page_idx)
                if item and item.icon().isNull():
                    item.setIcon(self._thumbnail_icon_for_page(page_idx))

    def _sync_thumbnail_selection(self):
        if not self.doc:
            return
        selected = set(self._thumbnail_selected_pages or {self.current_page_index})
        self._updating_thumbnail_list = True
        try:
            self.thumbnail_list.clearSelection()
            for row in range(self.thumbnail_list.count()):
                item = self.thumbnail_list.item(row)
                if item.data(Qt.UserRole) in selected:
                    item.setSelected(True)
        finally:
            self._updating_thumbnail_list = False

    def _thumbnail_scroll_hint_ensure_visible(self):
        hint = getattr(QAbstractItemView, "EnsureVisible", None)
        if hint is not None:
            return hint
        return QAbstractItemView.ScrollHint.EnsureVisible

    def _ensure_current_thumbnail_visible(self):
        if not self.doc:
            return
        if self.current_page_index < 0 or self.current_page_index >= self.thumbnail_list.count():
            return
        item = self.thumbnail_list.item(self.current_page_index)
        if item is None:
            return
        self.thumbnail_list.scrollToItem(item, self._thumbnail_scroll_hint_ensure_visible())

    def _refresh_thumbnail_sidebar(self, force: bool = False):
        if not self.doc or not self.base_path:
            self._updating_thumbnail_list = True
            try:
                self.thumbnail_list.clear()
            finally:
                self._updating_thumbnail_list = False
            return

        doc_sig = len(self.doc)
        if force or self._thumbnail_doc_sig != doc_sig or self.thumbnail_list.count() != len(self.doc):
            restore_scroll_value = self._restore_thumbnail_scroll_value
            self._restore_thumbnail_scroll_value = None
            self._updating_thumbnail_list = True
            try:
                self.thumbnail_list.setUpdatesEnabled(False)
                self.thumbnail_list.clear()
                for idx in range(len(self.doc)):
                    item = QListWidgetItem(f"{idx + 1}")
                    item.setData(Qt.UserRole, idx)
                    item.setTextAlignment(Qt.AlignCenter)
                    item.setSizeHint(self._thumbnail_item_size)
                    self.thumbnail_list.addItem(item)
                self._thumbnail_doc_sig = doc_sig
            finally:
                self.thumbnail_list.setUpdatesEnabled(True)
                self._updating_thumbnail_list = False
            if restore_scroll_value is not None:
                bar = self.thumbnail_list.verticalScrollBar()
                bar.setValue(max(bar.minimum(), min(bar.maximum(), int(restore_scroll_value))))
        else:
            self._restore_thumbnail_scroll_value = None

        self._sync_thumbnail_selection()
        self._ensure_thumbnail_icons()
        self._update_page_action_ui()

    def _selected_page_indices(self) -> List[int]:
        selected = sorted(self._thumbnail_selected_pages)
        if selected:
            return selected
        if self.doc:
            return [self.current_page_index]
        return []

    def _set_current_page_selection(self, page_indices: List[int], current_page_index: Optional[int] = None):
        if not page_indices:
            self._thumbnail_selected_pages = set()
            self._update_page_action_ui()
            return
        self._thumbnail_selected_pages = set(page_indices)
        if current_page_index is not None:
            self.current_page_index = current_page_index
        elif self.current_page_index not in self._thumbnail_selected_pages:
            self.current_page_index = min(page_indices)
        self._update_page_action_ui()

    def on_thumbnail_selection_changed(self):
        if self._updating_thumbnail_list:
            return
        selected = sorted(
            self.thumbnail_list.item(row).data(Qt.UserRole)
            for row in range(self.thumbnail_list.count())
            if self.thumbnail_list.item(row).isSelected()
        )
        if not selected:
            self._set_current_page_selection([self.current_page_index], self.current_page_index)
            self._sync_thumbnail_selection()
            self._update_page_action_ui()
            return
        self._thumbnail_selected_pages = set(selected)
        self._update_page_action_ui()
        target_page = selected[0]
        if target_page != self.current_page_index:
            self.current_page_index = target_page
            self._scroll_to_current_after_render = True
            self.render_page()

    def _on_thumbnail_scroll(self):
        self._ensure_thumbnail_icons()

    def map_scene_to_page_point(self, scene_pos) -> Tuple[Optional[int], Optional[fitz.Point]]:
        for page_idx, (x0, y0, width, height) in self._page_scene_layouts.items():
            if x0 <= scene_pos.x() <= x0 + width and y0 <= scene_pos.y() <= y0 + height:
                return page_idx, fitz.Point((scene_pos.x() - x0) / self.zoom, (scene_pos.y() - y0) / self.zoom)
        return None, None

    def _rebuild_scene_layouts(self):
        self._page_scene_layouts = {}
        if not self.doc:
            return
        if self.continuous_view:
            x0 = self._scene_padding_x
            y0 = self._scene_padding_y
            for page_idx in range(len(self.doc)):
                rect = self.doc[page_idx].rect
                width = float(rect.width * self.zoom)
                height = float(rect.height * self.zoom)
                self._page_scene_layouts[page_idx] = (x0, y0, width, height)
                y0 += height + self._scene_page_gap
        else:
            rect = self.doc[self.current_page_index].rect
            self._page_scene_layouts[self.current_page_index] = (
                0.0,
                0.0,
                float(rect.width * self.zoom),
                float(rect.height * self.zoom),
            )

    def _visible_scene_page_indices(self) -> List[int]:
        if not self.doc:
            return []
        if not self.continuous_view:
            return [self.current_page_index]
        if not self._page_scene_layouts:
            self._rebuild_scene_layouts()
        if self._scroll_to_current_after_render:
            start = max(0, self.current_page_index - 1)
            end = min(len(self.doc), self.current_page_index + 2)
            return list(range(start, end))
        if self._pending_viewport_anchor:
            anchor_page_idx = int(self._pending_viewport_anchor[0])
            start = max(0, anchor_page_idx - 1)
            end = min(len(self.doc), anchor_page_idx + 2)
            return list(range(start, end))
        view_rect = self.view.mapToScene(self.view.viewport().rect()).boundingRect()
        pad = max(600.0, float(view_rect.height()))
        top = view_rect.top() - pad
        bottom = view_rect.bottom() + pad
        visible = []
        for page_idx, (_, y0, _, height) in self._page_scene_layouts.items():
            if y0 <= bottom and (y0 + height) >= top:
                visible.append(page_idx)
        if self.current_page_index not in visible:
            visible.append(self.current_page_index)
        visible.sort()
        return visible or [self.current_page_index]

    def queue_visible_refresh(self):
        if not self._has_open_doc():
            return
        if self.continuous_view:
            self._visible_render_timer.start(75)
        else:
            self._ensure_thumbnail_icons()

    def _refresh_visible_after_scroll(self):
        if not self._has_open_doc():
            return
        self.render_page()

    def _scroll_to_current_page(self):
        layout = self._page_scene_layouts.get(self.current_page_index)
        if not layout:
            return
        x0, y0, width, height = layout
        self.view.horizontalScrollBar().setValue(int(x0))
        self.view.verticalScrollBar().setValue(int(y0))

    def _center_page_fraction(self, page_idx: int, fx: float, fy: float):
        layout = self._page_scene_layouts.get(page_idx)
        if not layout:
            return
        x0, y0, width, height = layout
        scene_x = float(x0) + (max(0.0, min(1.0, fx)) * float(width))
        scene_y = float(y0) + (max(0.0, min(1.0, fy)) * float(height))
        target_x = int(round(scene_x - (self.view.viewport().width() * 0.5)))
        target_y = int(round(scene_y - (self.view.viewport().height() * 0.5)))

        hbar = self.view.horizontalScrollBar()
        vbar = self.view.verticalScrollBar()
        hbar.setValue(max(hbar.minimum(), min(hbar.maximum(), target_x)))
        vbar.setValue(max(vbar.minimum(), min(vbar.maximum(), target_y)))

    def _scroll_page_point_to_viewport(self, page_idx: int, page_x: float, page_y: float, viewport_x: float, viewport_y: float):
        layout = self._page_scene_layouts.get(page_idx)
        if not layout:
            return
        x0, y0, _, _ = layout
        scene_x = float(x0) + (float(page_x) * float(self.zoom))
        scene_y = float(y0) + (float(page_y) * float(self.zoom))
        target_x = int(round(scene_x - float(viewport_x)))
        target_y = int(round(scene_y - float(viewport_y)))
        hbar = self.view.horizontalScrollBar()
        vbar = self.view.verticalScrollBar()
        hbar.setValue(max(hbar.minimum(), min(hbar.maximum(), target_x)))
        vbar.setValue(max(vbar.minimum(), min(vbar.maximum(), target_y)))

    def _page_anchor_from_scene_pos(self, scene_pos) -> Tuple[Optional[int], Optional[fitz.Point]]:
        if not self._has_open_doc():
            return None, None
        page_idx, page_point = self.map_scene_to_page_point(scene_pos)
        if page_idx is not None and page_point is not None:
            return page_idx, page_point

        if not self._page_scene_layouts:
            self._rebuild_scene_layouts()
        layout = self._page_scene_layouts.get(self.current_page_index)
        if not layout:
            return None, None

        x0, y0, _, _ = layout
        page_rect = self.doc[self.current_page_index].rect
        page_x = (float(scene_pos.x()) - float(x0)) / max(0.001, float(self.zoom))
        page_y = (float(scene_pos.y()) - float(y0)) / max(0.001, float(self.zoom))
        page_x = max(0.0, min(float(page_rect.width), page_x))
        page_y = max(0.0, min(float(page_rect.height), page_y))
        return self.current_page_index, fitz.Point(page_x, page_y)

    def _capture_viewport_anchor(self, viewport_pos=None) -> Optional[Tuple[int, float, float, float, float]]:
        if not self._has_open_doc():
            return None
        viewport = self.view.viewport()
        if viewport_pos is None:
            center = viewport.rect().center()
            viewport_x = float(center.x())
            viewport_y = float(center.y())
        else:
            try:
                viewport_x = float(viewport_pos.x())
                viewport_y = float(viewport_pos.y())
            except Exception:
                center = viewport.rect().center()
                viewport_x = float(center.x())
                viewport_y = float(center.y())

        max_viewport_x = max(0.0, float(viewport.width() - 1))
        max_viewport_y = max(0.0, float(viewport.height() - 1))
        viewport_x = max(0.0, min(max_viewport_x, viewport_x))
        viewport_y = max(0.0, min(max_viewport_y, viewport_y))
        scene_pos = self.view.mapToScene(QPoint(int(round(viewport_x)), int(round(viewport_y))))
        page_idx, page_point = self._page_anchor_from_scene_pos(scene_pos)
        if page_idx is None or page_point is None:
            return None
        return (
            int(page_idx),
            float(page_point.x),
            float(page_point.y),
            float(viewport_x),
            float(viewport_y),
        )

    def _apply_pending_thumbnail_focus(self) -> bool:
        if not self._pending_thumbnail_focus:
            return False
        page_idx, fx, fy = self._pending_thumbnail_focus
        if page_idx not in self._page_scene_layouts:
            return False
        self._center_page_fraction(page_idx, fx, fy)
        self._pending_thumbnail_focus = None
        return True

    def _apply_pending_viewport_anchor(self) -> bool:
        if not self._pending_viewport_anchor:
            return False
        page_idx, page_x, page_y, viewport_x, viewport_y = self._pending_viewport_anchor
        if page_idx not in self._page_scene_layouts:
            return False
        self._scroll_page_point_to_viewport(page_idx, page_x, page_y, viewport_x, viewport_y)
        self._pending_viewport_anchor = None
        return True


    def open_pdf(self):
        path, _ = QFileDialog.getOpenFileName(self, "PDF 열기", "", "PDF files (*.pdf)")
        if not path:
            return
        self.load_pdf(Path(path), reset_edits=True)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            urls = event.mimeData().urls()
            if urls and urls[0].toLocalFile().lower().endswith(".pdf"):
                event.acceptProposedAction()
                return
        event.ignore()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if urls:
            file_path = urls[0].toLocalFile()
            if file_path.lower().endswith(".pdf"):
                self.load_pdf(Path(file_path), reset_edits=True)
                event.acceptProposedAction()

    def load_pdf(self, path: Path, reset_edits: bool = True):
        try:
            old_temp = self.temp_margin_file
            old_base = self.base_path

            self.original_path = Path(path)
            self.base_path = Path(path)
            self.temp_margin_file = None

            self.doc = fitz.open(str(self.base_path))
            if self.doc.needs_pass:
                pw, ok = QInputDialog.getText(
                    self, "비밀번호", "PDF 비밀번호를 입력하세요:",
                    QLineEdit.EchoMode.Password
                )
                if not ok or not self.doc.authenticate(pw):
                    QMessageBox.warning(self, "열기 실패", "비밀번호가 올바르지 않습니다.")
                    self.doc.close()
                    self.doc = None
                    return
            self._page_words_cache.clear()
            self._page_norm_words_cache.clear()
            self._page_word_first_index_cache.clear()
            self._reference_rect_index.clear()
            self._reference_index_sig = None
            self._invalidate_render_cache()
            self._invalidate_thumbnail_cache()
            self.current_page_index = 0
            self._thumbnail_selected_pages = {0}
            self._pending_thumbnail_focus = None
            self._pending_viewport_anchor = None
            self._scroll_thumbnail_to_current_after_render = False
            self._restore_thumbnail_scroll_value = None
            self._registration_mark_pick_mode = False
            self._update_registration_mark_pick_cursor()

            if reset_edits:
                self.link_edits.clear()
                self.new_links.clear()
                self.link_deletes.clear()
                self.annotations.clear()
                self.undo_stack.clear()
                self.redo_stack.clear()
                self.modified = False
                self._clear_search_state()
                self._cleanup_obsolete_temp_files(keep={self.original_path, self.base_path})

            keep_paths = {self.original_path, self.base_path}
            if old_temp and Path(old_temp) not in keep_paths:
                self._cleanup_temp_file(Path(old_temp))
            if old_base and old_base != old_temp and Path(old_base) not in keep_paths:
                self._cleanup_temp_file(Path(old_base))

            self._refresh_thumbnail_sidebar(force=True)
            self._refresh_bookmark_tree()
            self._update_page_action_ui()
            self.render_page()
        except Exception as e:
            QMessageBox.critical(self, "열기 실패", f"오류가 발생했습니다:\n{e}")

    # ---------------- Rendering / Extract ----------------

    def _invalidate_render_cache(self):
        self._render_pix_cache.clear()
        self._render_span_cache.clear()
        self._render_cache_order.clear()
        self._page_words_cache.clear()
        self._page_norm_words_cache.clear()
        self._page_word_first_index_cache.clear()
        self._reference_rect_index.clear()
        self._reference_index_sig = None
        self._page_base_spans_cache.clear()
        self.current_spans = []
        self.current_spans_by_page = {}
        self._page_scene_layouts = {}
        self._pending_thumbnail_focus = None
        self._pending_viewport_anchor = None
        self._scroll_thumbnail_to_current_after_render = False
        self._restore_thumbnail_scroll_value = None
        self._registration_mark_pick_mode = False
        self._update_registration_mark_pick_cursor()

    def _cache_key(self, page_idx: int) -> Tuple:
        base_key = str(self.base_path)
        return (base_key, int(page_idx), float(self.zoom), round(float(self._page_render_oversample), 2))

    def _cache_get(self, page_idx: int):
        key = self._cache_key(page_idx)
        pix = self._render_pix_cache.get(key)
        spans = self._render_span_cache.get(key)
        if pix is None or spans is None:
            return None, None
        return pix, spans

    def _cache_put(self, page_idx: int, pixmap: QPixmap, spans: List[SpanInfo]):
        key = self._cache_key(page_idx)
        if key in self._render_pix_cache:
            self._render_pix_cache[key] = pixmap
            self._render_span_cache[key] = spans
            return
        self._render_pix_cache[key] = pixmap
        self._render_span_cache[key] = spans
        self._render_cache_order.append(key)
        while len(self._render_cache_order) > self._render_cache_max:
            old = self._render_cache_order.pop(0)
            self._render_pix_cache.pop(old, None)
            self._render_span_cache.pop(old, None)

    def _add_overlay_rect(self, page_idx: int, rect: fitz.Rect, pen: QPen, brush: QBrush):
        r = fitz.Rect(rect)
        offset = self._page_scene_layouts.get(page_idx, (0.0, 0.0, 0.0, 0.0))
        x0 = offset[0] + (r.x0 * self.zoom)
        y0 = offset[1] + (r.y0 * self.zoom)
        w = r.width * self.zoom
        h = r.height * self.zoom
        self.scene.addRect(x0, y0, w, h, pen, brush)

    def _normalize_extracted_char(self, font_name: str, char: str) -> str:
        if not char:
            return ""
        return self.text_edit_support.decode_symbol_font_char(font_name, char)

    def _span_space_metrics(self, raw_chars: List[dict], font_size: float) -> Tuple[float, float]:
        size = max(2.0, float(font_size or 11.0))
        positive_gaps: List[float] = []
        prev_bbox = None
        prev_origin_y = None

        for ch in raw_chars or []:
            cb = ch.get("bbox")
            if not cb:
                continue
            ch_origin_raw = ch.get("origin", (cb[0], cb[3]))
            ch_origin_y = float(ch_origin_raw[1])
            if prev_bbox is not None:
                gap = float(cb[0]) - float(prev_bbox[2])
                baseline_delta = abs(ch_origin_y - float(prev_origin_y or ch_origin_y))
                if gap > 0 and baseline_delta <= max(1.0, size * 0.35):
                    positive_gaps.append(float(gap))
            prev_bbox = cb
            prev_origin_y = ch_origin_y

        if not positive_gaps:
            return max(0.65, size * 0.08), max(1.2, size * 0.22)

        positive_gaps.sort()
        sample = positive_gaps[:max(1, (len(positive_gaps) * 2 + 2) // 3)]
        normal_gap = sample[len(sample) // 2]
        trigger = max(0.65, size * 0.08, normal_gap * 1.7)
        approx_space = max(trigger, size * 0.22, normal_gap * 2.15)
        return trigger, approx_space

    def _synthetic_space_count(
        self,
        font_size: float,
        gap: float,
        trigger: Optional[float] = None,
        approx_space: Optional[float] = None,
    ) -> int:
        gap = float(gap or 0.0)
        size = max(2.0, float(font_size or 11.0))
        gap_trigger = float(trigger) if trigger is not None else max(0.65, size * 0.08)
        space_width = float(approx_space) if approx_space is not None else max(1.2, size * 0.22)
        if gap < gap_trigger:
            return 0
        count = int(round(gap / max(1e-6, space_width)))
        return max(1, min(3, count))

    def _extract_spans(self, page: fitz.Page) -> List[SpanInfo]:
        spans: List[SpanInfo] = []
        text_dict = page.get_text("rawdict")
        line_idx, span_idx = 0, 0

        def compact_vertical_bounds(baseline_y: float, font_size: float, ascender: float, descender: float):
            size = max(2.0, float(font_size or 11.0))
            asc = float(ascender or 0.0)
            desc = float(descender or 0.0)
            top_ratio = 0.74
            bottom_ratio = 0.22
            if asc > 0:
                top_ratio = min(0.82, max(0.68, asc * 0.88))
            if desc < 0:
                bottom_ratio = min(0.30, max(0.18, (-desc) * 0.58))
            top = float(baseline_y) - (size * top_ratio)
            bottom = float(baseline_y) + (size * bottom_ratio)
            if bottom <= top:
                bottom = top + max(2.0, size * 0.72)
            return top, bottom

        for block in text_dict.get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    bbox = span.get("bbox")
                    raw_chars = span.get("chars", [])
                    if not bbox or not raw_chars:
                        continue

                    chars: List[CharInfo] = []
                    text_parts: List[str] = []
                    span_origin = fitz.Point(span.get("origin", (bbox[0], bbox[3])))
                    span_asc = float(span.get("ascender", 0.0) or 0.0)
                    span_desc = float(span.get("descender", 0.0) or 0.0)
                    span_size = float(span.get("size", 11.0) or 11.0)
                    span_font_name = span.get("font", "Helvetica")
                    space_trigger, approx_space = self._span_space_metrics(raw_chars, span_size)
                    prev_right = None
                    prev_origin_y = None
                    for ch in raw_chars:
                        c = self._normalize_extracted_char(span_font_name, str(ch.get("c", "")))
                        cb = ch.get("bbox")
                        if not c or not cb:
                            continue
                        ch_origin_raw = ch.get("origin", (cb[0], span_origin.y))
                        ch_origin = fitz.Point(float(ch_origin_raw[0]), float(ch_origin_raw[1]))
                        top, bottom = compact_vertical_bounds(span_origin.y, span_size, span_asc, span_desc)
                        if prev_right is not None:
                            gap = float(cb[0]) - float(prev_right)
                            baseline_delta = abs(float(ch_origin.y) - float(prev_origin_y or ch_origin.y))
                            if baseline_delta <= max(1.0, span_size * 0.35):
                                space_count = self._synthetic_space_count(
                                    span_size,
                                    gap,
                                    trigger=space_trigger,
                                    approx_space=approx_space,
                                )
                                if space_count > 0:
                                    step = gap / float(space_count)
                                    for idx in range(space_count):
                                        sx0 = float(prev_right) + (step * idx)
                                        sx1 = float(prev_right) + (step * (idx + 1))
                                        space_rect = fitz.Rect(sx0, top, sx1, bottom)
                                        chars.append(
                                            CharInfo(
                                                char=" ",
                                                rect=space_rect,
                                                raw_rect=fitz.Rect(space_rect),
                                                origin=fitz.Point(sx0, float(span_origin.y)),
                                            )
                                        )
                                        text_parts.append(" ")
                        compact_rect = fitz.Rect(float(cb[0]), top, float(cb[2]), bottom)
                        raw_rect = fitz.Rect(float(cb[0]), float(cb[1]), float(cb[2]), float(cb[3]))
                        chars.append(CharInfo(char=c, rect=compact_rect, raw_rect=raw_rect, origin=ch_origin))
                        text_parts.append(c)
                        prev_right = float(cb[2])
                        prev_origin_y = float(ch_origin.y)

                    text = "".join(text_parts)
                    if not text.strip():
                        continue

                    char_rects = [c.rect for c in chars]
                    tight_rect = fitz.Rect(char_rects[0])
                    for r in char_rects[1:]:
                        tight_rect |= r

                    spans.append(
                        SpanInfo(
                            page_index=page.number,
                            line_index=line_idx,
                            span_index=span_idx,
                            rect=tight_rect,
                            raw_rect=fitz.Rect(float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])),
                            text=text,
                            origin=span_origin,
                            chars=chars,
                            font_name=span_font_name,
                            font_size=span.get("size", 11.0),
                            color=span.get("color", 0),
                            ascender=float(span.get("ascender", 0.0) or 0.0),
                            descender=float(span.get("descender", 0.0) or 0.0),
                        )
                    )
                    span_idx += 1
                line_idx += 1
        return spans

    def _get_page_base_spans(self, page_idx: int) -> List[SpanInfo]:
        cache_key = (str(self.base_path), int(page_idx))
        spans = self._page_base_spans_cache.get(cache_key)
        if spans is not None:
            return spans
        try:
            spans = self._extract_spans(self.doc[page_idx])
        except Exception:
            spans = []
        self._page_base_spans_cache[cache_key] = spans
        return spans

    def render_page(self):
        if not self._has_open_doc():
            return

        self._rebuild_scene_layouts()
        page_indices = self._visible_scene_page_indices()
        missing_page_indices = [page_idx for page_idx in page_indices if self._cache_get(page_idx)[0] is None]

        if missing_page_indices:
            for page_idx in missing_page_indices:
                page = self.doc[page_idx]
                spans = self._get_page_base_spans(page_idx)
                render_scale = float(self.zoom) * float(self._page_render_oversample)
                pix = page.get_pixmap(matrix=fitz.Matrix(render_scale, render_scale), alpha=False)
                image = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format_RGB888)
                pixmap = QPixmap.fromImage(image)
                pixmap.setDevicePixelRatio(float(self._page_render_oversample))
                self._cache_put(page_idx, pixmap, spans)

        self.scene.clear()
        self.current_spans_by_page = {}
        max_width = 0.0
        max_bottom = 0.0

        for page_idx in page_indices:
            pixmap, spans = self._cache_get(page_idx)
            if pixmap is None or spans is None:
                continue
            x0, y0, width, height = self._page_scene_layouts.get(page_idx, (0.0, 0.0, 0.0, 0.0))
            item = self.scene.addPixmap(pixmap)
            item.setPos(x0, y0)
            self.current_spans_by_page[page_idx] = spans
            page_border = QPen(QColor(210, 210, 210))
            page_border.setWidth(1)
            self.scene.addRect(x0, y0, width, height, page_border)
            max_width = max(max_width, x0 + width + self._scene_padding_x)
            max_bottom = max(max_bottom, y0 + height + self._scene_padding_y)

        self.current_spans = self.current_spans_by_page.get(self.current_page_index, [])
        if not max_width or not max_bottom:
            if self._page_scene_layouts:
                last_idx = len(self.doc) - 1
                x0, y0, width, height = self._page_scene_layouts[last_idx]
                max_width = x0 + width + self._scene_padding_x
                max_bottom = y0 + height + self._scene_padding_y
        self.scene.setSceneRect(0, 0, max_width, max_bottom)

        no_pen = QPen(Qt.NoPen)

        if self.show_links_btn.isChecked():
            try:
                for page_idx in page_indices:
                    page = self.doc[page_idx]
                    for lnk in page.get_links():
                        kind = lnk.get("kind")
                        if kind == fitz.LINK_GOTO:
                            brush = QBrush(QColor(255, 204, 0, 80))
                            pen = QPen(QColor(255, 204, 0, 180))
                        elif kind == fitz.LINK_URI:
                            brush = QBrush(QColor(0, 180, 80, 80))
                            pen = QPen(QColor(0, 180, 80, 180))
                        else:
                            continue
                        r = fitz.Rect(lnk["from"])
                        if any(d.page_index == page_idx and fitz.Rect(d.link_rect).intersects(r) for d in self.link_deletes):
                            continue
                        pen.setWidth(1)
                        self._add_overlay_rect(page_idx, r, pen, brush)
            except Exception:
                pass

        if self.search_results:
            for idx2, (p_idx, rect) in enumerate(self.search_results):
                if p_idx not in self._page_scene_layouts:
                    continue
                if idx2 == self.search_index:
                    brush = QBrush(QColor(0, 204, 204, 120))
                else:
                    brush = QBrush(QColor(0, 204, 204, 60))
                self._add_overlay_rect(p_idx, rect, no_pen, brush)

        for nl in self.new_links:
            if nl.page_index not in self._page_scene_layouts:
                continue
            brush = QBrush(QColor(0, 0, 255, 60))
            pen = QPen(QColor(0, 0, 255, 160))
            pen.setWidth(1)
            self._add_overlay_rect(nl.page_index, nl.rect, pen, brush)

        annot_colors = {
            "highlight": QColor(255, 255, 0, 80),
            "underline": QColor(0, 150, 255, 80),
            "strikeout": QColor(255, 80, 80, 80),
        }
        for ann in self.annotations:
            if ann.page_index not in self._page_scene_layouts:
                continue
            color = annot_colors.get(ann.annot_type, QColor(255, 255, 0, 80))
            brush = QBrush(color)
            pen = QPen(color.darker(120))
            pen.setWidth(1)
            self._add_overlay_rect(ann.page_index, ann.rect, pen, brush)

        page_num = self.current_page_index + 1
        total = len(self.doc)
        pending_lnk = len([l for l in self.new_links if l.page_index == self.current_page_index])

        name = self.original_path.name if self.original_path else "(no file)"
        self.info_label.setText(f"{name} (새 링크: {pending_lnk}건)")
        self.page_input.setText(str(page_num))
        self.total_page_label.setText(f"/ {total}")
        self._refresh_thumbnail_sidebar(force=False)
        if self._scroll_thumbnail_to_current_after_render:
            self._ensure_current_thumbnail_visible()
            self._scroll_thumbnail_to_current_after_render = False
        if self._apply_pending_thumbnail_focus():
            self._scroll_to_current_after_render = False
            if self.continuous_view:
                self._visible_render_timer.start(0)
        elif self._apply_pending_viewport_anchor():
            self._scroll_to_current_after_render = False
            if self.continuous_view:
                self._visible_render_timer.start(0)
        elif self._scroll_to_current_after_render:
            self._scroll_to_current_page()
            self._scroll_to_current_after_render = False
            if self.continuous_view:
                self._visible_render_timer.start(0)

    # ---------------- Apply links ----------------

    def _apply_edits_to_doc(self, target_doc: fitz.Document, preview_only: bool = False):
        if preview_only:
            return

        for d in self.link_deletes:
            page = target_doc[d.page_index]
            drect = fitz.Rect(d.link_rect)
            for lnk in list(page.get_links()):
                if lnk.get("kind") not in (fitz.LINK_GOTO, fitz.LINK_URI):
                    continue
                lr = fitz.Rect(lnk["from"])
                if self._significant_overlap(lr, drect, threshold=0.2) or drect.contains(lr.tl) or drect.contains(lr.br) or lr.contains(drect.tl) or lr.contains(drect.br):
                    try:
                        page.delete_link(lnk)
                    except Exception:
                        pass

        for le in self.link_edits:
            page = target_doc[le.page_index]
            target_rect = fitz.Rect(le.link_rect)
            for lnk in list(page.get_links()):
                if lnk.get("kind") != fitz.LINK_GOTO:
                    continue
                lr = fitz.Rect(lnk["from"])
                if self._significant_overlap(lr, target_rect, threshold=0.2) or target_rect.contains(lr.tl) or target_rect.contains(lr.br) or lr.contains(target_rect.tl) or lr.contains(target_rect.br):
                    try:
                        page.delete_link(lnk)
                    except Exception:
                        pass
                    new_lnk = dict(lnk)
                    new_lnk["page"] = le.new_page
                    new_lnk["from"] = le.link_rect
                    page.insert_link(new_lnk)
                    break

        for nl in self.new_links:
            page = target_doc[nl.page_index]
            if nl.uri:
                page.insert_link({
                    "kind": fitz.LINK_URI,
                    "from": nl.rect,
                    "uri": nl.uri,
                })
            else:
                page.insert_link({
                    "kind": fitz.LINK_GOTO,
                    "from": nl.rect,
                    "page": nl.target_page,
                })

    # ---------------- Actions ----------------

    def toggle_show_links(self, checked: bool):
        if checked:
            self.show_links_btn.setText("기존 링크 표시: ON")
            self.show_links_btn.setStyleSheet(
                "background-color: #ffcc00; color: #000000; font-weight: bold; border-radius: 4px; padding: 4px;"
            )
        else:
            self.show_links_btn.setText("기존 링크 표시: OFF")
            self.show_links_btn.setStyleSheet(
                "background-color: #fff2cc; color: #664d00; border: 1px solid #ffcc00; border-radius: 4px; padding: 4px;"
            )
        self.render_page()

    def toggle_continuous_view(self, checked: bool):
        self.continuous_view = bool(checked)
        self._update_view_toggle_button()
        if hasattr(self, "continuous_view_action"):
            self.continuous_view_action.blockSignals(True)
            self.continuous_view_action.setChecked(self.continuous_view)
            self.continuous_view_action.blockSignals(False)
        self._scroll_to_current_after_render = True
        self.render_page()

    def zoom_in(self):
        if self.zoom_index < len(self.zoom_levels) - 1:
            next_idx = self.zoom_index + 1
            self._set_zoom(self.zoom_levels[next_idx], zoom_index=next_idx)

    def zoom_out(self):
        if self.zoom_index > 0:
            next_idx = self.zoom_index - 1
            self._set_zoom(self.zoom_levels[next_idx], zoom_index=next_idx)

    def _set_zoom(self, new_zoom: float, anchor_viewport_pos=None, zoom_index: Optional[int] = None):
        if not self._has_open_doc():
            return
        old_zoom = float(self.zoom)
        clamped_zoom = max(0.45, min(5.5, float(new_zoom)))
        if abs(clamped_zoom - old_zoom) < 0.01:
            return
        self._pending_viewport_anchor = self._capture_viewport_anchor(anchor_viewport_pos)
        self.zoom = clamped_zoom
        if zoom_index is None:
            zoom_index = min(range(len(self.zoom_levels)), key=lambda i: abs(self.zoom_levels[i] - clamped_zoom))
        self.zoom_index = int(zoom_index)
        self._scroll_to_current_after_render = False
        self.render_page()

    def adjust_zoom_by_factor(self, factor: float, anchor_viewport_pos=None):
        if not self._has_open_doc():
            return
        if factor <= 0:
            return
        old_zoom = float(self.zoom)
        new_zoom = round(old_zoom * factor, 3)
        self._set_zoom(new_zoom, anchor_viewport_pos=anchor_viewport_pos)

    def focus_search(self):
        self.search_input.setFocus()
        self.search_input.selectAll()

    def clear_search(self):
        if not self.search_query and not self.search_results and not self.search_input.text():
            return
        self._clear_search_state()
        self.render_page()

    # ---------------- Search ----------------

    def perform_search(self, force: bool = False) -> bool:
        if not self.doc:
            return False

        query = self._current_search_text()
        if not query:
            self.search_query = ""
            self._clear_search_matches(rerender=True)
            return False

        if force or query != self.search_query or not self.search_results:
            self.search_query = query
            self.search_results.clear()
            if self._is_reference_query(query):
                terms = self._expand_reference_terms(query)
                words_cache: Dict[int, list] = {}
                for i in range(len(self.doc)):
                    rects = self._find_reference_rects_on_page(i, terms, words_cache=words_cache)
                    for r in rects:
                        self.search_results.append((i, r))
            else:
                for i in range(len(self.doc)):
                    rects = self.doc[i].search_for(query)
                    for r in rects:
                        self.search_results.append((i, r))

            self.search_index = -1
            for i, (p_idx, _) in enumerate(self.search_results):
                if p_idx >= self.current_page_index:
                    self.search_index = i
                    break
            if self.search_index == -1 and self.search_results:
                self.search_index = 0

        self.update_search_ui()
        return bool(self.search_results)

    def update_search_ui(self):
        if not self.search_results:
            self.search_label.setText("결과 없음")
            self._update_search_action_ui()
            self.render_page()
            return

        if self.search_index < 0 or self.search_index >= len(self.search_results):
            self.search_index = 0
        self.search_label.setText(f"{self.search_index + 1} / {len(self.search_results)}")
        self._update_search_action_ui()
        target_page = self.search_results[self.search_index][0]
        if self.current_page_index != target_page:
            self.current_page_index = target_page
        self.render_page()

    def next_search_result(self):
        query = self._current_search_text()
        if not query:
            return
        had_current_results = bool(self.search_results) and query == self.search_query
        if not self.perform_search():
            return
        if had_current_results and self.search_results:
            self.search_index = (self.search_index + 1) % len(self.search_results)
            self.update_search_ui()

    def prev_search_result(self):
        query = self._current_search_text()
        if not query:
            return
        had_current_results = bool(self.search_results) and query == self.search_query
        if not self.perform_search():
            return
        if not had_current_results and self.search_results:
            self.search_index = (self.search_index - 1) % len(self.search_results)
            self.update_search_ui()
            return
        self.search_index = (self.search_index - 1) % len(self.search_results)
        self.update_search_ui()

    def first_search_result(self):
        query = self._current_search_text()
        if not query:
            return
        if not self.perform_search():
            return
        self.search_index = 0
        self.update_search_ui()

    # ---------------- Simple text edit ----------------

    def edit_text_at_point(self, click_point: fitz.Point, page_index: Optional[int] = None, select_line: bool = False) -> bool:
        return self.text_edit_support.edit_text_at_point(click_point, page_index, select_line)

    # ---------------- Link helpers ----------------

    def _rect_area(self, r: fitz.Rect) -> float:
        return max(0.0, float(r.width)) * max(0.0, float(r.height))

    def _rect_intersection_area(self, a: fitz.Rect, b: fitz.Rect) -> float:
        try:
            inter = a & b
            if inter.is_empty:
                return 0.0
            return self._rect_area(inter)
        except Exception:
            return 0.0

    def _significant_overlap(self, a: fitz.Rect, b: fitz.Rect, threshold: float = 0.55) -> bool:
        ia = self._rect_intersection_area(a, b)
        if ia <= 0:
            return False
        denom = min(self._rect_area(a), self._rect_area(b))
        if denom <= 0:
            return False
        return (ia / denom) >= threshold

    def _expand_auto_link_terms(self, raw: str) -> List[str]:
        s = (raw or "").strip()
        if not s:
            return []

        if "~~" in s:
            parts = [p.strip() for p in s.split("~~", 1)]
        elif "～" in s:
            parts = [p.strip() for p in s.split("～", 1)]
        elif "~" in s:
            parts = [p.strip() for p in s.split("~", 1)]
        else:
            return [s]

        if len(parts) != 2 or not parts[0] or not parts[1]:
            return [s]

        left, right = parts[0], parts[1]

        if re.match(r"^\d+\.\d+$", right):
            m = re.match(r"^(.*?)(\d+\.\d+)$", left)
            if m:
                right = m.group(1) + right

        pat = re.compile(r"^(?P<prefix>.*?)(?P<a>\d+)\.(?P<b>\d+)$")
        m1 = pat.match(left)
        m2 = pat.match(right)
        if not (m1 and m2):
            return [s]

        prefix1 = (m1.group("prefix") or "")
        prefix2 = (m2.group("prefix") or "")
        a1, b1 = int(m1.group("a")), int(m1.group("b"))
        a2, b2 = int(m2.group("a")), int(m2.group("b"))

        if a1 != a2:
            return [s]
        if prefix1.strip() != prefix2.strip():
            return [s]

        start, end = (b1, b2) if b1 <= b2 else (b2, b1)
        prefix = prefix1
        return [f"{prefix}{a1}.{i}" for i in range(start, end + 1)]

    def _canonical_reference_term(self, term: str) -> Optional[Tuple[str, str]]:
        s = re.sub(r"\s+", " ", (term or "").strip())
        if not s:
            return None

        m = re.match(
            r"^(equation|equations|eq\.?|figure|figures|fig\.?|table|tables|tbl\.?)\s+(\d+\.\d+)$",
            s,
            flags=re.IGNORECASE,
        )
        if not m:
            return None

        prefix = (m.group(1) or "").lower().rstrip(".")
        if prefix in {"equation", "equations", "eq"}:
            kind = "equation"
        elif prefix in {"figure", "figures", "fig"}:
            kind = "figure"
        elif prefix in {"table", "tables", "tbl"}:
            kind = "table"
        else:
            return None

        return kind, m.group(2)

    def _reference_key_for_term(self, term: str) -> Optional[str]:
        canonical = self._canonical_reference_term(term)
        if not canonical:
            return None
        kind, num = canonical
        return f"{kind} {num}"

    def _scaled_prefix_rect(self, rect: fitz.Rect, full_token: str, prefix_token: str) -> fitz.Rect:
        r = fitz.Rect(rect)
        full = (full_token or "").strip()
        prefix = (prefix_token or "").strip()
        if not full or not prefix or len(prefix) >= len(full) or r.width <= 0:
            return r
        ratio = max(0.15, min(1.0, len(prefix) / len(full)))
        return fitz.Rect(r.x0, r.y0, r.x0 + (r.width * ratio), r.y1)

    def _ensure_reference_index(self):
        if not self.doc or not self.base_path:
            self._reference_rect_index.clear()
            self._reference_index_sig = None
            return

        sig = (str(self.base_path), len(self.doc))
        if self._reference_index_sig == sig:
            return

        alias_kind_map = {
            "equation": "equation",
            "equations": "equation",
            "eq": "equation",
            "figure": "figure",
            "figures": "figure",
            "fig": "figure",
            "table": "table",
            "tables": "table",
            "tbl": "table",
        }
        num_pat = re.compile(r"^(?P<num>\d+\.\d+)(?P<suffix>[a-z]+)?$")

        index: Dict[str, Dict[int, List[fitz.Rect]]] = {}

        for page_idx in range(len(self.doc)):
            page = self.doc[page_idx]
            words, norm_words, _ = self._get_cached_page_words_data(page)
            if len(words) < 2:
                continue

            for i in range(len(words) - 1):
                kind = alias_kind_map.get(norm_words[i])
                if not kind:
                    continue

                next_token = norm_words[i + 1]
                m = num_pat.match(next_token)
                if not m:
                    continue

                num = m.group("num")
                prefix_rect = fitz.Rect(words[i][0], words[i][1], words[i][2], words[i][3])
                number_rect = fitz.Rect(words[i + 1][0], words[i + 1][1], words[i + 1][2], words[i + 1][3])
                if next_token != num:
                    number_rect = self._scaled_prefix_rect(number_rect, next_token, num)

                rect = fitz.Rect(prefix_rect)
                rect |= number_rect

                key = f"{kind} {num}"
                by_page = index.setdefault(key, {})
                rects = by_page.setdefault(page_idx, [])
                self._append_rect_dedup(rects, rect, threshold=0.95)

        self._reference_rect_index = index
        self._reference_index_sig = sig

    def _expand_reference_terms(self, raw: str) -> List[str]:
        base_terms = self._expand_auto_link_terms(raw)
        terms: List[str] = []
        seen: Set[str] = set()
        for base_term in base_terms:
            for alias_term in self._expand_link_alias_terms(base_term):
                key = alias_term.lower().strip()
                if key in seen:
                    continue
                seen.add(key)
                terms.append(alias_term)
        return terms

    def _expand_link_alias_terms(self, term: str) -> List[str]:
        s = (term or "").strip()
        if not s:
            return []

        canonical = self._canonical_reference_term(s)
        if not canonical:
            return [s]

        kind, num = canonical
        alias_map = {
            "equation": [f"equation {num}", f"equations {num}", f"eq. {num}", f"eq {num}"],
            "figure": [f"figure {num}", f"figures {num}", f"fig. {num}", f"fig {num}"],
            "table": [f"table {num}", f"tables {num}", f"tbl. {num}", f"tbl {num}"],
        }
        variants = alias_map.get(kind, [s])

        seen = set()
        out: List[str] = []
        for v in variants:
            key = v.lower().strip()
            if key in seen:
                continue
            seen.add(key)
            out.append(v)
        return out

    def _normalize_found_text(self, s: str) -> str:
        return re.sub(r"\s+", " ", (s or "")).strip()

    def _normalize_word_token(self, w: str) -> str:
        return (w or "").strip().strip(",.;:()[]{}").lower()

    def _is_whole_term_match(self, extracted: str, term: str) -> bool:
        e = self._normalize_found_text(extracted).lower()
        t = self._normalize_found_text(term).lower()

        if not e or not t:
            return False
        if not e.startswith(t):
            return False
        if len(e) == len(t):
            return True

        tail = e[len(t):]
        if re.match(r"^[\s,.;:()\[\]{}]*\d", tail):
            return False

        return True

    def _get_cached_page_words_data(self, page: fitz.Page) -> Tuple[list, List[str], Dict[str, List[int]]]:
        cache_key = (str(self.base_path), int(page.number))
        words = self._page_words_cache.get(cache_key)
        norm_words = self._page_norm_words_cache.get(cache_key)
        first_index = self._page_word_first_index_cache.get(cache_key)
        if words is None or norm_words is None or first_index is None:
            try:
                words = page.get_text("words")
            except Exception:
                words = []
            norm_words = [self._normalize_word_token(w[4]) for w in words]
            first_index = {}
            for idx, token in enumerate(norm_words):
                if not token:
                    continue
                first_index.setdefault(token, []).append(idx)
            self._page_words_cache[cache_key] = words
            self._page_norm_words_cache[cache_key] = norm_words
            self._page_word_first_index_cache[cache_key] = first_index
        return words, norm_words, first_index

    def _find_term_rects_by_words(
        self,
        page: fitz.Page,
        term: str,
        words: Optional[list] = None,
        norm_words: Optional[List[str]] = None,
        first_index: Optional[Dict[str, List[int]]] = None,
    ) -> List[fitz.Rect]:
        tokens = (term or "").strip().split()
        tokens = [t.lower() for t in tokens]
        if not tokens:
            return []

        canonical = self._canonical_reference_term(term)

        if words is None or norm_words is None or first_index is None:
            words, norm_words, first_index = self._get_cached_page_words_data(page)

        rects: List[fitz.Rect] = []

        n = len(tokens)
        if not words or len(words) < n:
            return rects

        candidate_indices = first_index.get(tokens[0], [])
        for i in candidate_indices:
            if i + n > len(words):
                continue
            ok = True
            for j in range(n):
                w = norm_words[i + j]
                if j == n - 1 and canonical and re.fullmatch(r"\d+\.\d+", tokens[j]):
                    if w == tokens[j]:
                        continue
                    if re.fullmatch(re.escape(tokens[j]) + r"[a-z]+", w):
                        continue
                    ok = False
                    break
                if w != tokens[j]:
                    ok = False
                    break
            if not ok:
                continue

            last_token = tokens[-1]
            if re.fullmatch(r"\d+\.\d+", last_token) and not canonical:
                next_idx = i + n
                if next_idx < len(words):
                    next_word = norm_words[next_idx]
                    if next_word.isdigit():
                        continue

            r = fitz.Rect(words[i][0], words[i][1], words[i][2], words[i][3])
            for j in range(1, n):
                r |= fitz.Rect(words[i + j][0], words[i + j][1], words[i + j][2], words[i + j][3])
            rects.append(r)

        return rects

    def _append_rect_dedup(self, rects: List[fitz.Rect], rect: fitz.Rect, threshold: float = 0.90):
        new_rect = fitz.Rect(rect)
        for existing in rects:
            if self._significant_overlap(existing, new_rect, threshold=threshold):
                return
        rects.append(new_rect)

    def _find_reference_rects_on_page(
        self,
        page_idx: int,
        terms: List[str],
        words_cache: Optional[Dict[int, Tuple[list, List[str], Dict[str, List[int]]]]] = None,
    ) -> List[fitz.Rect]:
        if not self.doc or not (0 <= page_idx < len(self.doc)):
            return []

        page = self.doc[page_idx]

        if words_cache is None:
            words_cache = {}

        keys: List[str] = []
        seen_keys: Set[str] = set()
        for term in terms:
            key = self._reference_key_for_term(term)
            if not key or key in seen_keys:
                continue
            seen_keys.add(key)
            keys.append(key)

        if keys:
            self._ensure_reference_index()
            page_rects: List[fitz.Rect] = []
            for key in keys:
                by_page = self._reference_rect_index.get(key, {})
                for rect in by_page.get(page_idx, []):
                    self._append_rect_dedup(page_rects, fitz.Rect(rect), threshold=0.90)
            return page_rects

        if page_idx not in words_cache:
            words_cache[page_idx] = self._get_cached_page_words_data(page)

        page_rects: List[fitz.Rect] = []
        words, norm_words, first_index = words_cache[page_idx]
        for term in terms:
            rects = self._find_term_rects_by_words(
                page,
                term,
                words=words,
                norm_words=norm_words,
                first_index=first_index,
            )
            for rect in rects:
                self._append_rect_dedup(page_rects, fitz.Rect(rect), threshold=0.90)

        return page_rects

    def _is_reference_query(self, raw: str) -> bool:
        for base_term in self._expand_auto_link_terms(raw):
            if self._canonical_reference_term(base_term):
                return True
        return False

    def _append_new_link_dedup(self, page_idx: int, rect: fitz.Rect, target_page_idx: int):
        new_rect = fitz.Rect(rect)
        for nl in self.new_links:
            if nl.page_index != page_idx:
                continue
            try:
                old_rect = fitz.Rect(nl.rect)
            except Exception:
                continue
            if self._significant_overlap(old_rect, new_rect, threshold=0.90):
                return

        self.new_links.append(NewLinkEntry(page_idx, new_rect, target_page_idx))

    # ---------------- Editing ----------------

    def edit_link_at_point(self, click_point: fitz.Point, page_index: Optional[int] = None) -> bool:
        if not self.doc:
            return False
        target_page_idx = self.current_page_index if page_index is None else page_index

        idx = self._find_pending_new_link_at_point(target_page_idx, click_point)
        if idx is not None:
            nl = self.new_links[idx]

            dialog = QDialog(self)
            dialog.setWindowTitle("새 링크 수정")
            dialog.setFixedSize(360, 140)

            layout = QVBoxLayout(dialog)
            layout.addWidget(QLabel("저장되지 않은 새 링크입니다.\n이 링크가 이동할 페이지 번호를 입력하세요."))

            row = QHBoxLayout()
            row.addWidget(QLabel("페이지:"))
            page_edit = QLineEdit(str(int(nl.target_page) + 1))
            page_edit.setFixedWidth(80)
            page_edit.setAlignment(Qt.AlignCenter)
            row.addWidget(page_edit)
            row.addStretch(1)
            layout.addLayout(row)

            btns = QHBoxLayout()
            apply_btn = QPushButton("수정 적용")
            delete_btn = QPushButton("링크 제거")
            cancel_btn = QPushButton("취소")
            btns.addStretch(1)
            btns.addWidget(apply_btn)
            btns.addWidget(delete_btn)
            btns.addWidget(cancel_btn)
            layout.addLayout(btns)

            action = {"type": None, "page": None}

            def on_apply():
                try:
                    val = int(page_edit.text().strip())
                except Exception:
                    QMessageBox.warning(dialog, "입력 오류", "페이지 번호를 숫자로 입력하세요.")
                    return
                if not (1 <= val <= len(self.doc)):
                    QMessageBox.warning(dialog, "입력 오류", f"페이지 번호는 1 ~ {len(self.doc)} 범위여야 합니다.")
                    return
                action["type"] = "edit"
                action["page"] = val - 1
                dialog.accept()

            def on_delete():
                action["type"] = "delete"
                dialog.accept()

            apply_btn.clicked.connect(on_apply)
            delete_btn.clicked.connect(on_delete)
            cancel_btn.clicked.connect(dialog.reject)

            if dialog.exec() != QDialog.Accepted or not action["type"]:
                return True

            self._push_undo_snapshot()

            if action["type"] == "delete":
                try:
                    self.new_links.pop(idx)
                except Exception:
                    pass
                self._mark_modified()
                self._invalidate_render_cache()
                self.render_page()
                return True

            self.new_links[idx] = NewLinkEntry(
                page_index=nl.page_index,
                rect=nl.rect,
                target_page=int(action["page"]),
            )
            self._mark_modified()
            self._invalidate_render_cache()
            self.render_page()
            return True

        page = self.doc[target_page_idx]
        for lnk in page.get_links():
            if lnk.get("kind") != fitz.LINK_GOTO:
                continue

            try:
                link_rect = fitz.Rect(lnk["from"])
            except Exception:
                continue

            if not link_rect.contains(click_point):
                continue

            for d in self.link_deletes:
                if d.page_index == target_page_idx and fitz.Rect(d.link_rect).intersects(link_rect):
                    return True

            current_target = int(lnk.get("page", 0))
            for le in self.link_edits:
                if le.page_index == target_page_idx and fitz.Rect(le.link_rect).intersects(link_rect):
                    current_target = int(le.new_page)
                    break

            dialog = QDialog(self)
            dialog.setWindowTitle("기존 링크 수정")
            dialog.setFixedSize(360, 140)

            layout = QVBoxLayout(dialog)
            layout.addWidget(QLabel("이 링크가 이동할 페이지 번호를 입력하세요."))

            row = QHBoxLayout()
            row.addWidget(QLabel("페이지:"))
            page_edit = QLineEdit(str(current_target + 1))
            page_edit.setFixedWidth(80)
            page_edit.setAlignment(Qt.AlignCenter)
            row.addWidget(page_edit)
            row.addStretch(1)
            layout.addLayout(row)

            btns = QHBoxLayout()
            apply_btn = QPushButton("수정 적용")
            delete_btn = QPushButton("링크 제거")
            cancel_btn = QPushButton("취소")
            btns.addStretch(1)
            btns.addWidget(apply_btn)
            btns.addWidget(delete_btn)
            btns.addWidget(cancel_btn)
            layout.addLayout(btns)

            action = {"type": None, "page": None}

            def on_apply():
                try:
                    val = int(page_edit.text().strip())
                except Exception:
                    QMessageBox.warning(dialog, "입력 오류", "페이지 번호를 숫자로 입력하세요.")
                    return
                if not (1 <= val <= len(self.doc)):
                    QMessageBox.warning(dialog, "입력 오류", f"페이지 번호는 1 ~ {len(self.doc)} 범위여야 합니다.")
                    return
                action["type"] = "edit"
                action["page"] = val - 1
                dialog.accept()

            def on_delete():
                action["type"] = "delete"
                dialog.accept()

            apply_btn.clicked.connect(on_apply)
            delete_btn.clicked.connect(on_delete)
            cancel_btn.clicked.connect(dialog.reject)

            if dialog.exec() != QDialog.Accepted or not action["type"]:
                return True

            self._push_undo_snapshot()

            if action["type"] == "delete":
                self.link_edits = [
                    le for le in self.link_edits
                    if not (le.page_index == target_page_idx and fitz.Rect(le.link_rect).intersects(link_rect))
                ]
                self.link_deletes.append(LinkDeleteEntry(target_page_idx, link_rect))
                self._mark_modified()
                self._invalidate_render_cache()
                self.render_page()
                return True

            new_target = int(action["page"])
            self.link_deletes = [
                d for d in self.link_deletes
                if not (d.page_index == target_page_idx and fitz.Rect(d.link_rect).intersects(link_rect))
            ]
            self.link_edits = [
                le for le in self.link_edits
                if not (le.page_index == target_page_idx and fitz.Rect(le.link_rect).intersects(link_rect))
            ]
            self.link_edits.append(LinkEditEntry(target_page_idx, link_rect, new_target))
            self._mark_modified()
            self._invalidate_render_cache()
            self.render_page()
            return True

        return False

    def remove_link_at_point(self, click_point: fitz.Point, page_index: Optional[int] = None) -> bool:
        if not self.doc:
            return False
        target_page_idx = self.current_page_index if page_index is None else page_index

        idx = self._find_pending_new_link_at_point(target_page_idx, click_point)
        if idx is not None:
            self._push_undo_snapshot()
            try:
                self.new_links.pop(idx)
            except Exception:
                pass
            self._mark_modified()
            self._invalidate_render_cache()
            self.render_page()
            return True

        page = self.doc[target_page_idx]
        for lnk in page.get_links():
            if lnk.get("kind") in (fitz.LINK_GOTO, fitz.LINK_URI) and fitz.Rect(lnk["from"]).contains(click_point):
                self._push_undo_snapshot()
                self.link_deletes.append(LinkDeleteEntry(target_page_idx, fitz.Rect(lnk["from"])))
                self._mark_modified()
                self._invalidate_render_cache()
                self.render_page()
                return True
        return False

    def add_new_link_at_point(self, click_point: fitz.Point, page_index: Optional[int] = None) -> bool:
        if not self.doc:
            return False
        target_page_idx = self.current_page_index if page_index is None else page_index
        spans = self.current_spans_by_page.get(target_page_idx, self.current_spans)

        target_rect = None

        for p_idx, rect in self.search_results:
            if p_idx == target_page_idx and rect.contains(click_point):
                target_rect = rect
                break

        if not target_rect:
            text_target = self.text_edit_support.find_text_target_at_point(target_page_idx, click_point, select_line=False)
            if text_target is not None:
                target_rect = self.text_edit_support.effective_target_rect(text_target, allow_overflow_right=False)

        if not target_rect:
            for span in spans:
                if span.rect.contains(click_point):
                    target_rect = span.rect
                    break

        if not target_rect:
            return False

        result = self._show_new_link_dialog()
        if result is None:
            return False

        self._push_undo_snapshot()

        page = self.doc[target_page_idx]
        rrect = fitz.Rect(target_rect)
        for lnk in page.get_links():
            if lnk.get("kind") not in (fitz.LINK_GOTO, fitz.LINK_URI):
                continue
            try:
                lr = fitz.Rect(lnk["from"])
            except Exception:
                continue
            if self._significant_overlap(lr, rrect, threshold=0.35) or (rrect.contains(lr.tl) and rrect.contains(lr.br)):
                if not any(
                    d.page_index == target_page_idx and self._significant_overlap(fitz.Rect(d.link_rect), lr, threshold=0.90)
                    for d in self.link_deletes
                ):
                    self.link_deletes.append(LinkDeleteEntry(target_page_idx, lr))

        if result["type"] == "uri":
            nl = NewLinkEntry(target_page_idx, target_rect, 0, uri=result["uri"])
            self.new_links.append(nl)
        else:
            self._append_new_link_dedup(target_page_idx, target_rect, result["page"])

        self._mark_modified()
        self._invalidate_render_cache()
        self.render_page()
        return True

    def _show_new_link_dialog(self) -> Optional[dict]:
        from PySide6.QtWidgets import QRadioButton, QButtonGroup
        dialog = QDialog(self)
        dialog.setWindowTitle("새 하이퍼링크 추가")
        dialog.setFixedSize(380, 200)
        layout = QVBoxLayout(dialog)

        radio_page = QRadioButton("페이지 이동")
        radio_url = QRadioButton("외부 URL")
        radio_page.setChecked(True)
        bg = QButtonGroup(dialog)
        bg.addButton(radio_page)
        bg.addButton(radio_url)
        layout.addWidget(radio_page)

        page_row = QHBoxLayout()
        page_row.addWidget(QLabel("페이지:"))
        page_input = QLineEdit(str(self.current_page_index + 1))
        page_input.setFixedWidth(80)
        page_row.addWidget(page_input)
        page_row.addStretch()
        layout.addLayout(page_row)

        layout.addWidget(radio_url)
        url_input = QLineEdit()
        url_input.setPlaceholderText("https://...")
        url_input.setEnabled(False)
        layout.addWidget(url_input)

        def _toggle(checked):
            page_input.setEnabled(radio_page.isChecked())
            url_input.setEnabled(radio_url.isChecked())
        radio_page.toggled.connect(_toggle)

        btn_row = QHBoxLayout()
        ok_btn = QPushButton("확인")
        cancel_btn = QPushButton("취소")
        ok_btn.clicked.connect(dialog.accept)
        cancel_btn.clicked.connect(dialog.reject)
        btn_row.addStretch()
        btn_row.addWidget(ok_btn)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

        if dialog.exec() != QDialog.Accepted:
            return None

        if radio_url.isChecked():
            uri = url_input.text().strip()
            if not uri:
                return None
            return {"type": "uri", "uri": uri}
        else:
            try:
                pg = int(page_input.text())
                if 1 <= pg <= len(self.doc):
                    return {"type": "page", "page": pg - 1}
            except ValueError:
                pass
            QMessageBox.warning(self, "오류", "유효한 페이지 번호를 입력하세요.")
            return None

    # ---------------- Auto link ----------------

    def auto_add_links(self):
        if not self.doc:
            QMessageBox.warning(self, "알림", "먼저 PDF 파일을 열어주세요.")
            return

        default_term = self.last_auto_link_term
        if default_term:
            m = re.search(r"(\d+)(?!.*\d)", default_term)
            if m:
                num = int(m.group(1)) + 1
                default_term = default_term[:m.start(1)] + str(num) + default_term[m.end(1):]

        search_text, ok = QInputDialog.getText(
            self,
            "자동 링크",
            "찾을 단어/범위 (예: equation 7.1 또는 equation 7.7 ~ 7.10):",
            text=default_term
        )
        if not ok or not (search_text or "").strip():
            return

        self.last_auto_link_term = search_text.strip()
        terms = self._expand_reference_terms(search_text)
        if not terms:
            return

        dialog = QDialog(self)
        dialog.setWindowTitle("자동 링크")
        dialog.setFixedSize(360, 170)

        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("이동할 페이지 번호를 입력하세요.\n(삭제하려면 '삭제'를 누르세요.)"))

        row = QHBoxLayout()
        row.addWidget(QLabel("페이지:"))
        page_edit = QLineEdit(str(self.current_page_index + 1))
        page_edit.setFixedWidth(90)
        page_edit.setAlignment(Qt.AlignCenter)
        row.addWidget(page_edit)
        row.addStretch(1)
        layout.addLayout(row)

        btns = QHBoxLayout()
        ok_btn = QPushButton("OK")
        cancel_btn = QPushButton("취소")
        delete_btn = QPushButton("삭제")
        btns.addStretch(1)
        btns.addWidget(ok_btn)
        btns.addWidget(cancel_btn)
        btns.addWidget(delete_btn)
        layout.addLayout(btns)

        action = {"mode": None, "page": None}

        def on_ok():
            try:
                val = int(page_edit.text().strip())
            except Exception:
                QMessageBox.warning(dialog, "입력 오류", "페이지 번호를 숫자로 입력하세요.")
                return
            if not (1 <= val <= len(self.doc)):
                QMessageBox.warning(dialog, "입력 오류", f"페이지 번호는 1 ~ {len(self.doc)} 범위여야 합니다.")
                return
            action["mode"] = "add"
            action["page"] = val - 1
            dialog.accept()

        def on_delete():
            action["mode"] = "delete"
            dialog.accept()

        ok_btn.clicked.connect(on_ok)
        delete_btn.clicked.connect(on_delete)
        cancel_btn.clicked.connect(dialog.reject)

        if dialog.exec() != QDialog.Accepted or not action["mode"]:
            return

        do_delete = (action["mode"] == "delete")
        target_page_idx = action["page"]

        self._push_undo_snapshot()
        total = 0

        words_cache: Dict[int, list] = {}

        for page_idx in range(len(self.doc)):
            page = self.doc[page_idx]
            rects = self._find_reference_rects_on_page(page_idx, terms, words_cache=words_cache)
            if not rects:
                continue

            for rect in rects:
                rrect = fitz.Rect(rect)

                if do_delete:
                    removed_pending_count = 0

                    for lnk in page.get_links():
                        if lnk.get("kind") != fitz.LINK_GOTO:
                            continue
                        try:
                            lr = fitz.Rect(lnk["from"])
                        except Exception:
                            continue

                        if lr.intersects(rrect) or rrect.contains(lr.tl) or lr.contains(rrect.tl):
                            if not any(
                                d.page_index == page_idx and fitz.Rect(d.link_rect).intersects(lr)
                                for d in self.link_deletes
                            ):
                                self.link_deletes.append(LinkDeleteEntry(page_idx, lr))
                                total += 1

                    kept: List[NewLinkEntry] = []
                    for nl in self.new_links:
                        if nl.page_index != page_idx:
                            kept.append(nl)
                            continue
                        try:
                            nlr = fitz.Rect(nl.rect)
                        except Exception:
                            kept.append(nl)
                            continue
                        if nlr.intersects(rrect) or rrect.intersects(nlr):
                            removed_pending_count += 1
                            continue
                        kept.append(nl)
                    self.new_links = kept
                    total += removed_pending_count

                else:
                    for lnk in page.get_links():
                        if lnk.get("kind") != fitz.LINK_GOTO:
                            continue
                        try:
                            lr = fitz.Rect(lnk["from"])
                        except Exception:
                            continue
                        if lr.intersects(rrect) or rrect.contains(lr.tl) or lr.contains(rrect.tl):
                            if not any(
                                d.page_index == page_idx and fitz.Rect(d.link_rect).intersects(lr)
                                for d in self.link_deletes
                            ):
                                self.link_deletes.append(LinkDeleteEntry(page_idx, lr))

                    before = len(self.new_links)
                    self._append_new_link_dedup(page_idx, rrect, int(target_page_idx))
                    total += (len(self.new_links) - before)

        if total > 0:
            self._mark_modified()

        self._invalidate_render_cache()
        self.render_page()

        if do_delete:
            if len(terms) == 1:
                QMessageBox.information(self, "완료", f"총 {total}개의 '{terms[0]}' 링크가 삭제 예약되었습니다! (저장 시 반영)")
            else:
                QMessageBox.information(self, "완료", f"총 {total}개의 링크가 삭제 예약되었습니다! (저장 시 반영)\n(범위: {terms[0]} ~ {terms[-1]})")
        else:
            if len(terms) == 1:
                QMessageBox.information(self, "완료", f"총 {total}개의 '{terms[0]}' 하이퍼링크가 추가되었습니다!")
            else:
                QMessageBox.information(self, "완료", f"총 {total}개의 하이퍼링크가 추가되었습니다!\n(범위: {terms[0]} ~ {terms[-1]})")

    # ---------------- Undo/Redo ----------------

    def undo_last_action(self):
        if not self.undo_stack:
            self.statusBar().showMessage("실행 취소할 작업이 없습니다.", 2000)
            return

        current_snapshot = (
            [LinkEditEntry(**vars(e)) for e in self.link_edits],
            [NewLinkEntry(**vars(e)) for e in self.new_links],
            [LinkDeleteEntry(**vars(e)) for e in self.link_deletes],
            self.base_path,
            self.temp_margin_file,
            self.current_page_index,
        )
        self.redo_stack.append(current_snapshot)

        prev_snapshot = self.undo_stack.pop()
        self._restore_snapshot(prev_snapshot)
        self._invalidate_render_cache()

        self._mark_modified()
        self.render_page()

    def redo_last_action(self):
        if not self.redo_stack:
            self.statusBar().showMessage("다시 실행할 작업이 없습니다.", 2000)
            return

        current_snapshot = (
            [LinkEditEntry(**vars(e)) for e in self.link_edits],
            [NewLinkEntry(**vars(e)) for e in self.new_links],
            [LinkDeleteEntry(**vars(e)) for e in self.link_deletes],
            self.base_path,
            self.temp_margin_file,
            self.current_page_index,
        )
        self.undo_stack.append(current_snapshot)

        next_snapshot = self.redo_stack.pop()
        self._restore_snapshot(next_snapshot)
        self._invalidate_render_cache()

        self._mark_modified()
        self.render_page()

    # ---------------- Save / Close confirm ----------------

    def save_pdf(self):
        if not self._has_open_doc():
            return
        if self.save_path:
            self._do_save(self.save_path)
        else:
            self.save_pdf_as()

    def save_pdf_as(self):
        if not self._has_open_doc():
            return

        default_name = self.original_path.with_stem(self.original_path.stem + "_re")
        save_path, _ = QFileDialog.getSaveFileName(
            self,
            "다른 이름으로 저장",
            str(default_name),
            "PDF files (*.pdf)"
        )
        if not save_path:
            return
        self._do_save(Path(save_path))

    def _do_save(self, target: Path):
        try:
            current_page = self.current_page_index
            src = self.base_path

            save_doc = fitz.open(str(src))
            self._apply_edits_to_doc(save_doc, preview_only=False)
            self._apply_annotations_to_doc(save_doc)
            save_doc.save(str(target))
            save_doc.close()

            self.save_path = target
            QMessageBox.information(self, "성공", "저장 완료!")
            self.load_pdf(target, reset_edits=True)

            if self.doc and 0 <= current_page < len(self.doc):
                self.current_page_index = current_page
                self.render_page()

            self.modified = False

        except Exception as e:
            import traceback
            QMessageBox.critical(self, "저장 오류", f"오류:\n{e}\n\n{traceback.format_exc()}")

    def closeEvent(self, event):
        if self.modified:
            ret = QMessageBox.question(
                self,
                "저장 확인",
                "저장되지 않은 변경사항이 있습니다.\n저장하시겠습니까?",
                QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel
            )
            if ret == QMessageBox.Save:
                self.save_pdf()
                if self.modified:
                    event.ignore()
                else:
                    event.accept()
            elif ret == QMessageBox.Discard:
                event.accept()
            else:
                event.ignore()
        else:
            event.accept()

        if event.isAccepted():
            keep_paths = {self.original_path}
            self._cleanup_obsolete_temp_files(keep=keep_paths)
            self._clear_page_clipboard()

    # ---------------- Navigation ----------------

    def prev_page(self):
        if self.doc and self.current_page_index > 0:
            self.current_page_index -= 1
            self._thumbnail_selected_pages = {self.current_page_index}
            self._scroll_to_current_after_render = self.continuous_view
            self._scroll_thumbnail_to_current_after_render = True
            self.render_page()

    def next_page(self):
        if self.doc and self.current_page_index < len(self.doc) - 1:
            self.current_page_index += 1
            self._thumbnail_selected_pages = {self.current_page_index}
            self._scroll_to_current_after_render = self.continuous_view
            self._scroll_thumbnail_to_current_after_render = True
            self.render_page()

    def go_to_page(self):
        if not self.doc:
            return
        try:
            target_page = int(self.page_input.text()) - 1
            if 0 <= target_page < len(self.doc):
                self.current_page_index = target_page
                self._thumbnail_selected_pages = {self.current_page_index}
                self._scroll_to_current_after_render = self.continuous_view
                self._scroll_thumbnail_to_current_after_render = True
                self.render_page()
        except ValueError:
            pass

    def _activate_temp_pdf(
        self,
        tmp_path: Path,
        old_base: Optional[Path],
        old_temp: Optional[Path],
        structure_changed: bool = True,
    ):
        new_doc = fitz.open(str(tmp_path))
        self._register_temp_file(Path(tmp_path))
        old_doc = self.doc
        self.base_path = tmp_path
        self.temp_margin_file = tmp_path
        self.doc = new_doc
        if old_doc is not None:
            try:
                old_doc.close()
            except Exception:
                pass
        self._invalidate_render_cache()
        self._invalidate_thumbnail_cache(structure_changed=structure_changed)

    def _build_page_clipboard(self, page_indices: List[int], cut_mode: bool) -> Optional[PageClipboard]:
        selected_pages = sorted(set(page_indices))
        if not selected_pages:
            return None

        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
        tmp_path = Path(tmp.name)
        tmp.close()
        self._register_temp_file(tmp_path)

        try:
            src_doc = fitz.open(str(self.base_path))
            clip_doc = fitz.open()
            for start_page, end_page in self._contiguous_ranges(selected_pages):
                clip_doc.insert_pdf(src_doc, from_page=start_page, to_page=end_page)
            clip_doc.save(str(tmp_path))
            clip_doc.close()
            src_doc.close()
        except Exception as e:
            self._cleanup_temp_file(tmp_path)
            QMessageBox.critical(self, "페이지 복사 실패", f"오류:\n{e}")
            return None

        page_map = {page_idx: local_idx for local_idx, page_idx in enumerate(selected_pages)}
        link_edits = [
            LinkEditEntry(
                page_index=page_map[e.page_index],
                link_rect=e.link_rect,
                new_page=e.new_page,
            )
            for e in self.link_edits
            if e.page_index in page_map
        ]
        new_links = [
            NewLinkEntry(
                page_index=page_map[e.page_index],
                rect=e.rect,
                target_page=e.target_page,
            )
            for e in self.new_links
            if e.page_index in page_map
        ]
        link_deletes = [
            LinkDeleteEntry(
                page_index=page_map[e.page_index],
                link_rect=e.link_rect,
            )
            for e in self.link_deletes
            if e.page_index in page_map
        ]

        return PageClipboard(
            pdf_path=tmp_path,
            source_page_indices=selected_pages,
            link_edits=link_edits,
            new_links=new_links,
            link_deletes=link_deletes,
            cut_mode=cut_mode,
        )

    def _remap_clipboard_after_cut(self, clipboard: PageClipboard, deleted_page_indices: List[int]):
        deleted_pages = sorted(set(deleted_page_indices))
        selected_set = set(clipboard.source_page_indices)

        def remap_target(target_page: int) -> Optional[int]:
            if target_page in selected_set:
                return target_page
            return self._remap_page_index_after_delete_set(target_page, deleted_pages)

        rebuilt_link_edits = []
        for entry in clipboard.link_edits:
            mapped_target = remap_target(entry.new_page)
            if mapped_target is None:
                continue
            rebuilt_link_edits.append(
                LinkEditEntry(entry.page_index, entry.link_rect, mapped_target)
            )
        clipboard.link_edits = rebuilt_link_edits

        rebuilt_new_links = []
        for entry in clipboard.new_links:
            mapped_target = remap_target(entry.target_page)
            if mapped_target is None:
                continue
            rebuilt_new_links.append(
                NewLinkEntry(entry.page_index, entry.rect, mapped_target)
            )
        clipboard.new_links = rebuilt_new_links

    def _map_clipboard_target_page(self, clipboard: PageClipboard, target_page: int, insert_at: int) -> int:
        if target_page in clipboard.source_page_indices:
            return insert_at + clipboard.source_page_indices.index(target_page)
        if target_page >= insert_at:
            return target_page + len(clipboard.source_page_indices)
        return target_page

    def _rotate_rect_clockwise(self, rect: fitz.Rect, page_width: float, page_height: float) -> fitz.Rect:
        r = fitz.Rect(rect)
        corners = [
            (float(r.x0), float(r.y0)),
            (float(r.x1), float(r.y0)),
            (float(r.x1), float(r.y1)),
            (float(r.x0), float(r.y1)),
        ]
        rotated = [(float(page_height) - y, x) for x, y in corners]
        xs = [p[0] for p in rotated]
        ys = [p[1] for p in rotated]
        new_width = float(page_height)
        new_height = float(page_width)
        return fitz.Rect(
            max(0.0, min(xs)),
            max(0.0, min(ys)),
            min(new_width, max(xs)),
            min(new_height, max(ys)),
        )

    def _rotate_pending_changes_clockwise(self, page_sizes: Dict[int, Tuple[float, float]]):
        for entry in self.link_edits:
            page_size = page_sizes.get(entry.page_index)
            if page_size is None:
                continue
            entry.link_rect = self._rotate_rect_clockwise(entry.link_rect, page_size[0], page_size[1])

        for entry in self.new_links:
            page_size = page_sizes.get(entry.page_index)
            if page_size is None:
                continue
            entry.rect = self._rotate_rect_clockwise(entry.rect, page_size[0], page_size[1])

        for entry in self.link_deletes:
            page_size = page_sizes.get(entry.page_index)
            if page_size is None:
                continue
            entry.link_rect = self._rotate_rect_clockwise(entry.link_rect, page_size[0], page_size[1])

    def _paste_page_clipboard(self, clipboard: PageClipboard, insert_at: int, keep_clipboard: bool):
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
        tmp_path = Path(tmp.name)
        tmp.close()
        self._register_temp_file(tmp_path)

        try:
            src_doc = fitz.open(str(self.base_path))
            clip_doc = fitz.open(str(clipboard.pdf_path))
            merged = fitz.open()
            if insert_at > 0:
                merged.insert_pdf(src_doc, from_page=0, to_page=insert_at - 1)
            merged.insert_pdf(clip_doc)
            if insert_at < len(src_doc):
                merged.insert_pdf(src_doc, from_page=insert_at, to_page=len(src_doc) - 1)
            merged.save(str(tmp_path))
            merged.close()
            clip_doc.close()
            src_doc.close()
        except Exception as e:
            self._cleanup_temp_file(tmp_path)
            QMessageBox.critical(self, "페이지 붙여넣기 실패", f"오류:\n{e}")
            return False

        old_base = self.base_path
        old_temp = self.temp_margin_file
        try:
            self._activate_temp_pdf(tmp_path, old_base, old_temp)
        except Exception as e:
            self._cleanup_temp_file(tmp_path)
            QMessageBox.critical(self, "열기 실패", f"임시 PDF 열기 실패:\n{e}")
            return False

        self._rebuild_pending_changes_after_insert(insert_at, len(clipboard.source_page_indices))
        for entry in clipboard.link_edits:
            self.link_edits.append(
                LinkEditEntry(
                    page_index=insert_at + entry.page_index,
                    link_rect=entry.link_rect,
                    new_page=self._map_clipboard_target_page(clipboard, entry.new_page, insert_at),
                )
            )
        for entry in clipboard.new_links:
            self.new_links.append(
                NewLinkEntry(
                    page_index=insert_at + entry.page_index,
                    rect=entry.rect,
                    target_page=self._map_clipboard_target_page(clipboard, entry.target_page, insert_at),
                )
            )
        for entry in clipboard.link_deletes:
            self.link_deletes.append(
                LinkDeleteEntry(
                    page_index=insert_at + entry.page_index,
                    link_rect=entry.link_rect,
                )
            )

        inserted_pages = list(range(insert_at, insert_at + len(clipboard.source_page_indices)))
        self._set_current_page_selection(inserted_pages, inserted_pages[0])
        self._clear_search_state()
        self._mark_modified()
        self._refresh_thumbnail_sidebar(force=True)
        self._scroll_to_current_after_render = self.continuous_view
        self.render_page()

        if clipboard.cut_mode and not keep_clipboard:
            self._clear_page_clipboard()
        return True

    def delete_pages(self, page_indices: List[int], push_undo: bool = True):
        if not self._has_open_doc():
            QMessageBox.warning(self, "알림", "먼저 PDF 파일을 열어주세요.")
            return False

        selected_pages = sorted(set(page_indices))
        if not selected_pages:
            return False
        if len(selected_pages) >= len(self.doc):
            QMessageBox.warning(self, "경고", "페이지가 1장뿐이라 삭제할 수 없습니다.")
            return False

        if push_undo:
            self._push_undo_snapshot()

        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
        tmp_path = Path(tmp.name)
        tmp.close()
        self._register_temp_file(tmp_path)

        try:
            src_doc = fitz.open(str(self.base_path))
            delete_ranges = list(reversed(self._contiguous_ranges(selected_pages)))
            if hasattr(src_doc, "delete_pages"):
                for start_page, end_page in delete_ranges:
                    src_doc.delete_pages(start_page, end_page)
            else:
                for start_page, end_page in delete_ranges:
                    for page_idx in range(end_page, start_page - 1, -1):
                        src_doc.delete_page(page_idx)
            src_doc.save(str(tmp_path), garbage=4, deflate=True)
            src_doc.close()
        except Exception as e:
            self._cleanup_temp_file(tmp_path)
            QMessageBox.critical(self, "페이지 삭제 실패", f"오류:\n{e}")
            return False

        old_base = self.base_path
        old_temp = self.temp_margin_file
        try:
            self._activate_temp_pdf(tmp_path, old_base, old_temp)
        except Exception as e:
            self._cleanup_temp_file(tmp_path)
            QMessageBox.critical(self, "열기 실패", f"임시 PDF 열기 실패:\n{e}")
            return False

        self._rebuild_pending_changes_after_page_delete_many(selected_pages)
        self.current_page_index = min(selected_pages[0], len(self.doc) - 1)
        self._set_current_page_selection([self.current_page_index], self.current_page_index)
        self._clear_search_state()
        self._mark_modified()
        self._refresh_thumbnail_sidebar(force=True)
        self._scroll_to_current_after_render = self.continuous_view
        self.render_page()
        self.statusBar().showMessage(f"페이지 {len(selected_pages)}개를 삭제했습니다.", 2500)
        return True

    def delete_selected_pages(self):
        self.delete_pages(self._selected_page_indices())

    def delete_current_page(self):
        self.delete_pages([self.current_page_index])

    def copy_selected_pages(self):
        if not self._has_open_doc():
            QMessageBox.warning(self, "알림", "먼저 PDF 파일을 열어주세요.")
            return
        clipboard = self._build_page_clipboard(self._selected_page_indices(), cut_mode=False)
        if clipboard is None:
            return
        self._set_page_clipboard(clipboard)
        self.statusBar().showMessage(f"페이지 {len(clipboard.source_page_indices)}개를 복사했습니다.", 2500)

    def cut_selected_pages(self):
        if not self._has_open_doc():
            QMessageBox.warning(self, "알림", "먼저 PDF 파일을 열어주세요.")
            return
        selected_pages = self._selected_page_indices()
        if len(selected_pages) >= len(self.doc):
            QMessageBox.warning(self, "경고", "페이지가 1장뿐이라 잘라낼 수 없습니다.")
            return

        clipboard = self._build_page_clipboard(selected_pages, cut_mode=True)
        if clipboard is None:
            return

        self._push_undo_snapshot()
        self._set_page_clipboard(clipboard)
        self._remap_clipboard_after_cut(clipboard, selected_pages)
        if self.delete_pages(selected_pages, push_undo=False):
            self.statusBar().showMessage(f"페이지 {len(selected_pages)}개를 잘라냈습니다.", 2500)

    def paste_pages_after_selection(self):
        if not self._has_open_doc():
            QMessageBox.warning(self, "알림", "먼저 PDF 파일을 열어주세요.")
            return
        if not self.page_clipboard:
            QMessageBox.warning(self, "알림", "먼저 페이지를 복사하거나 잘라내세요.")
            return
        selected_pages = self._selected_page_indices()
        insert_at = (selected_pages[-1] + 1) if selected_pages else (self.current_page_index + 1)
        self._push_undo_snapshot()
        if self._paste_page_clipboard(self.page_clipboard, insert_at, keep_clipboard=not self.page_clipboard.cut_mode):
            self.statusBar().showMessage(f"페이지 {len(self._selected_page_indices())}개를 붙여넣었습니다.", 2500)

    def duplicate_selected_pages(self):
        if not self._has_open_doc():
            QMessageBox.warning(self, "알림", "먼저 PDF 파일을 열어주세요.")
            return
        selected_pages = self._selected_page_indices()
        clipboard = self._build_page_clipboard(selected_pages, cut_mode=False)
        if clipboard is None:
            return
        self._push_undo_snapshot()
        ok = self._paste_page_clipboard(clipboard, selected_pages[-1] + 1, keep_clipboard=True)
        self._cleanup_temp_file(clipboard.pdf_path)
        if ok:
            self.statusBar().showMessage(f"페이지 {len(selected_pages)}개를 복제했습니다.", 2500)

    # ---------------- Insert pages ----------------

    def insert_blank_page(self):
        if not self._has_open_doc():
            QMessageBox.warning(self, "알림", "먼저 PDF 파일을 열어주세요.")
            return
        page = self.doc[self.current_page_index]
        w, h = page.rect.width, page.rect.height

        tmp = Path(tempfile.gettempdir()) / f"goodpdf_blank_{id(self)}.pdf"
        blank_doc = fitz.open()
        blank_doc.new_page(width=w, height=h)
        blank_doc.save(str(tmp))
        blank_doc.close()

        clipboard = PageClipboard(
            pdf_path=tmp, source_page_indices=[0],
            link_edits=[], new_links=[], link_deletes=[], cut_mode=False,
        )
        insert_at = self.current_page_index + 1
        self._push_undo_snapshot()
        if self._paste_page_clipboard(clipboard, insert_at, keep_clipboard=False):
            self.statusBar().showMessage("빈 페이지를 삽입했습니다.", 2500)
        self._cleanup_temp_file(tmp)

    def insert_pages_from_pdf(self):
        if not self._has_open_doc():
            QMessageBox.warning(self, "알림", "먼저 PDF 파일을 열어주세요.")
            return

        path, _ = QFileDialog.getOpenFileName(self, "PDF 선택", "", "PDF files (*.pdf)")
        if not path:
            return

        try:
            ext_doc = fitz.open(path)
            if ext_doc.needs_pass:
                pw, ok = QInputDialog.getText(self, "비밀번호", "PDF 비밀번호:", QLineEdit.EchoMode.Password)
                if not ok or not ext_doc.authenticate(pw):
                    QMessageBox.warning(self, "오류", "비밀번호가 올바르지 않습니다.")
                    ext_doc.close()
                    return
        except Exception as e:
            QMessageBox.critical(self, "열기 실패", f"오류:\n{e}")
            return

        total = len(ext_doc)
        text, ok = QInputDialog.getText(
            self, "페이지 범위",
            f"삽입할 페이지 범위 (1-{total}):\n예: 1-5, 3, 7",
            text=f"1-{total}"
        )
        if not ok or not text.strip():
            ext_doc.close()
            return

        pages = self._parse_page_range(text.strip(), total)
        if not pages:
            QMessageBox.warning(self, "오류", "유효한 페이지 범위를 입력하세요.")
            ext_doc.close()
            return

        tmp = Path(tempfile.gettempdir()) / f"goodpdf_insert_{id(self)}.pdf"
        tmp_doc = fitz.open()
        for p in pages:
            tmp_doc.insert_pdf(ext_doc, from_page=p, to_page=p)
        tmp_doc.save(str(tmp))
        tmp_doc.close()
        ext_doc.close()

        clipboard = PageClipboard(
            pdf_path=tmp, source_page_indices=list(range(len(pages))),
            link_edits=[], new_links=[], link_deletes=[], cut_mode=False,
        )
        insert_at = self.current_page_index + 1
        self._push_undo_snapshot()
        if self._paste_page_clipboard(clipboard, insert_at, keep_clipboard=False):
            self.statusBar().showMessage(f"{len(pages)}개 페이지를 삽입했습니다.", 2500)
        self._cleanup_temp_file(tmp)

    @staticmethod
    def _parse_page_range(text: str, total: int) -> List[int]:
        pages = []
        for part in text.split(","):
            part = part.strip()
            if "-" in part:
                try:
                    a, b = part.split("-", 1)
                    start, end = int(a.strip()), int(b.strip())
                    pages.extend(range(max(1, start) - 1, min(total, end)))
                except ValueError:
                    continue
            else:
                try:
                    p = int(part)
                    if 1 <= p <= total:
                        pages.append(p - 1)
                except ValueError:
                    continue
        return pages

    # ---------------- Export images ----------------

    def export_pages_as_images(self):
        if not self._has_open_doc():
            QMessageBox.warning(self, "알림", "먼저 PDF 파일을 열어주세요.")
            return

        selected = self._selected_page_indices()
        if len(selected) <= 1:
            selected = [self.current_page_index]

        dpi, ok = QInputDialog.getInt(self, "DPI 설정", "내보내기 DPI:", 300, 72, 600)
        if not ok:
            return

        scale = dpi / 72.0

        if len(selected) == 1:
            default_name = f"{self.original_path.stem}_page{selected[0]+1}.png"
            save_path, _ = QFileDialog.getSaveFileName(
                self, "이미지 저장", default_name, "PNG (*.png);;JPEG (*.jpg)"
            )
            if not save_path:
                return
            page = self.doc[selected[0]]
            pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale))
            pix.save(save_path)
            QMessageBox.information(self, "완료", f"이미지를 저장했습니다:\n{save_path}")
        else:
            dir_path = QFileDialog.getExistingDirectory(self, "저장 폴더 선택")
            if not dir_path:
                return
            for idx in selected:
                page = self.doc[idx]
                pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale))
                out = Path(dir_path) / f"{self.original_path.stem}_page{idx+1}.png"
                pix.save(str(out))
            QMessageBox.information(self, "완료", f"{len(selected)}개 페이지를 이미지로 저장했습니다.")

    # ---------------- Bookmarks ----------------

    def _refresh_bookmark_tree(self):
        self.bookmark_tree.clear()
        if not self.doc:
            return
        try:
            toc = self.doc.get_toc()
        except Exception:
            return
        if not toc:
            item = QTreeWidgetItem(self.bookmark_tree, ["(북마크 없음)"])
            item.setData(0, Qt.UserRole, -1)
            return

        stack: List[QTreeWidgetItem] = []
        for level, title, page in toc:
            item = QTreeWidgetItem()
            item.setText(0, title)
            item.setData(0, Qt.UserRole, page - 1)

            while len(stack) >= level:
                stack.pop()

            if stack:
                stack[-1].addChild(item)
            else:
                self.bookmark_tree.addTopLevelItem(item)
            stack.append(item)

        self.bookmark_tree.expandAll()

    def _on_bookmark_clicked(self, item: QTreeWidgetItem, column: int):
        page = item.data(0, Qt.UserRole)
        if page is not None and page >= 0 and self.doc and page < len(self.doc):
            self.current_page_index = page
            self._thumbnail_selected_pages = {page}
            self.render_page()

    def _on_bookmark_context_menu(self, pos):
        if not self.doc:
            return
        menu = QMenu(self)
        add_action = menu.addAction("북마크 추가")
        item = self.bookmark_tree.itemAt(pos)
        edit_action = delete_action = None
        if item and item.data(0, Qt.UserRole) is not None and item.data(0, Qt.UserRole) >= 0:
            edit_action = menu.addAction("편집")
            delete_action = menu.addAction("삭제")

        action = menu.exec(self.bookmark_tree.viewport().mapToGlobal(pos))
        if action == add_action:
            self._add_bookmark()
        elif action == edit_action and item:
            self._edit_bookmark(item)
        elif action == delete_action and item:
            self._delete_bookmark(item)

    def _add_bookmark(self):
        title, ok = QInputDialog.getText(self, "북마크 추가", "북마크 이름:")
        if not ok or not title.strip():
            return
        page = self.current_page_index + 1
        toc = self.doc.get_toc() or []
        toc.append([1, title.strip(), page])
        self.doc.set_toc(toc)
        self._mark_modified()
        self._refresh_bookmark_tree()

    def _edit_bookmark(self, item: QTreeWidgetItem):
        old_title = item.text(0)
        old_page = item.data(0, Qt.UserRole)
        title, ok = QInputDialog.getText(self, "북마크 편집", "이름:", text=old_title)
        if not ok or not title.strip():
            return
        toc = self.doc.get_toc() or []
        for entry in toc:
            if entry[1] == old_title and entry[2] == old_page + 1:
                entry[1] = title.strip()
                break
        self.doc.set_toc(toc)
        self._mark_modified()
        self._refresh_bookmark_tree()

    def _delete_bookmark(self, item: QTreeWidgetItem):
        title = item.text(0)
        page = item.data(0, Qt.UserRole)
        toc = self.doc.get_toc() or []
        toc = [e for e in toc if not (e[1] == title and e[2] == page + 1)]
        self.doc.set_toc(toc)
        self._mark_modified()
        self._refresh_bookmark_tree()

    # ---------------- Annotations ----------------

    def _set_annotation_mode(self, mode: Optional[str]):
        self.annotation_mode = mode
        self.highlight_btn.setChecked(mode == "highlight")
        self.underline_btn.setChecked(mode == "underline")
        self.strikeout_btn.setChecked(mode == "strikeout")
        style_on = "background-color: #ffcc00; font-weight: bold; border-radius: 4px; padding: 4px;"
        style_off = ""
        self.highlight_btn.setStyleSheet(style_on if mode == "highlight" else style_off)
        self.underline_btn.setStyleSheet(style_on if mode == "underline" else style_off)
        self.strikeout_btn.setStyleSheet(style_on if mode == "strikeout" else style_off)

    def add_annotation_at_point(self, click_point: fitz.Point, page_index: Optional[int] = None, select_line: bool = False) -> bool:
        if not self.doc or not self.annotation_mode:
            return False
        target_page_idx = self.current_page_index if page_index is None else page_index
        text_target = self.text_edit_support.find_text_target_at_point(target_page_idx, click_point, select_line=select_line)
        if text_target is None:
            return False
        target_rect = self.text_edit_support.effective_target_rect(text_target, allow_overflow_right=False)
        if not target_rect:
            return False

        self._push_undo_snapshot()
        self.annotations.append(AnnotationEntry(
            page_index=target_page_idx,
            rect=fitz.Rect(target_rect),
            annot_type=self.annotation_mode,
        ))
        self._mark_modified()
        self._invalidate_render_cache()
        self.render_page()
        return True

    def _apply_annotations_to_doc(self, target_doc: fitz.Document):
        for ann in self.annotations:
            if ann.page_index >= len(target_doc):
                continue
            page = target_doc[ann.page_index]
            r = fitz.Rect(ann.rect)
            try:
                if ann.annot_type == "highlight":
                    a = page.add_highlight_annot(quads=[r])
                elif ann.annot_type == "underline":
                    a = page.add_underline_annot(quads=[r])
                elif ann.annot_type == "strikeout":
                    a = page.add_strikeout_annot(quads=[r])
                else:
                    continue
                a.update()
            except Exception:
                pass

    def rotate_pages_clockwise(self, page_indices: List[int], push_undo: bool = True):
        if not self._has_open_doc():
            QMessageBox.warning(self, "알림", "먼저 PDF 파일을 열어주세요.")
            return False

        selected_pages = sorted(set(page_indices))
        if not selected_pages:
            return False

        if push_undo:
            self._push_undo_snapshot()

        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
        tmp_path = Path(tmp.name)
        tmp.close()
        self._register_temp_file(tmp_path)

        page_sizes: Dict[int, Tuple[float, float]] = {}
        try:
            src_doc = fitz.open(str(self.base_path))
            for page_idx in selected_pages:
                page = src_doc[page_idx]
                page_sizes[page_idx] = (float(page.rect.width), float(page.rect.height))
                current_rotation = int(page.rotation or 0)
                page.set_rotation((current_rotation + 90) % 360)
            src_doc.save(str(tmp_path))
            src_doc.close()
        except Exception as e:
            self._cleanup_temp_file(tmp_path)
            QMessageBox.critical(self, "페이지 회전 실패", f"오류:\n{e}")
            return False

        old_base = self.base_path
        old_temp = self.temp_margin_file
        try:
            self._activate_temp_pdf(tmp_path, old_base, old_temp, structure_changed=False)
        except Exception as e:
            self._cleanup_temp_file(tmp_path)
            QMessageBox.critical(self, "열기 실패", f"임시 PDF 열기 실패:\n{e}")
            return False

        self._rotate_pending_changes_clockwise(page_sizes)
        current_page = self.current_page_index if self.current_page_index in set(selected_pages) else selected_pages[0]
        self._set_current_page_selection(selected_pages, current_page)
        self._clear_search_state()
        self._mark_modified()
        self._scroll_to_current_after_render = self.continuous_view
        self._scroll_thumbnail_to_current_after_render = True
        self.render_page()
        self.statusBar().showMessage(f"페이지 {len(selected_pages)}개를 시계 방향으로 90도 회전했습니다.", 2500)
        return True

    def rotate_selected_pages_clockwise(self):
        self.rotate_pages_clockwise(self._selected_page_indices())

    def _coerce_float(self, value, default: float = 0.0) -> float:
        try:
            if value is None:
                return float(default)
            return float(value)
        except Exception:
            return float(default)

    def _rect_distance_sq_to_point(self, rect: fitz.Rect, point: fitz.Point) -> float:
        x = self._coerce_float(getattr(point, "x", None))
        y = self._coerce_float(getattr(point, "y", None))
        dx = 0.0
        dy = 0.0
        if x < float(rect.x0):
            dx = float(rect.x0) - x
        elif x > float(rect.x1):
            dx = x - float(rect.x1)
        if y < float(rect.y0):
            dy = float(rect.y0) - y
        elif y > float(rect.y1):
            dy = y - float(rect.y1)
        return (dx * dx) + (dy * dy)

    def _find_registration_mark_annot_rect_near_point(self, page: fitz.Page, click_point: fitz.Point) -> Tuple[Optional[fitz.Rect], float]:
        printer_mark_type = int(getattr(fitz, "PDF_ANNOT_PRINTER_MARK", -1))
        trap_net_type = int(getattr(fitz, "PDF_ANNOT_TRAP_NET", -1))
        removable_annot_types = {v for v in (printer_mark_type, trap_net_type) if v >= 0}

        best_rect = None
        best_score = None
        annot = page.first_annot
        max_distance_sq = 26.0 * 26.0
        while annot:
            next_annot = annot.next
            try:
                annot_type = int((annot.type or [None])[0])
            except Exception:
                annot_type = -1
            if annot_type not in removable_annot_types:
                annot = next_annot
                continue
            try:
                rect = fitz.Rect(annot.rect)
            except Exception:
                annot = next_annot
                continue
            if rect.is_empty:
                annot = next_annot
                continue
            dist_sq = self._rect_distance_sq_to_point(rect, click_point)
            contains = 0 if (rect + (-6, -6, 6, 6)).contains(click_point) else 1
            if contains and dist_sq > max_distance_sq:
                annot = next_annot
                continue
            score = (contains, dist_sq, self._rect_area(rect))
            if best_score is None or score < best_score:
                best_score = score
                best_rect = fitz.Rect(rect) + (-3.0, -3.0, 3.0, 3.0)
            annot = next_annot
        return best_rect, float(best_score[1]) if best_score is not None else float("inf")

    def _find_registration_mark_vector_rect_near_point(self, page: fitz.Page, click_point: fitz.Point) -> Tuple[Optional[fitz.Rect], float]:
        best_rect = None
        best_score = None
        max_distance_sq = 28.0 * 28.0

        for cluster in self._target_like_drawing_clusters(page):
            rect = fitz.Rect(cluster["rect"])
            dist_sq = self._rect_distance_sq_to_point(rect, click_point)
            contains = 0 if (rect + (-8, -8, 8, 8)).contains(click_point) else 1
            if contains and dist_sq > max_distance_sq:
                continue
            score = (contains, dist_sq, self._rect_area(rect))
            if best_score is None or score < best_score:
                best_score = score
                best_rect = fitz.Rect(rect) + (-4.0, -4.0, 4.0, 4.0)

        if best_rect is not None:
            return best_rect, float(best_score[1])

        try:
            drawings = page.get_drawings()
        except Exception:
            drawings = []

        for path in drawings:
            try:
                rect = fitz.Rect(path.get("rect"))
            except Exception:
                continue
            if rect.is_empty:
                continue
            width = float(rect.width)
            height = float(rect.height)
            if width <= 1.0 or height <= 1.0 or width > 72.0 or height > 72.0:
                continue
            dist_sq = self._rect_distance_sq_to_point(rect, click_point)
            contains = 0 if (rect + (-8, -8, 8, 8)).contains(click_point) else 1
            if contains and dist_sq > max_distance_sq:
                continue
            grow = max(4.0, self._coerce_float(path.get("width"), 1.0) * 3.0)
            score = (contains, dist_sq, self._rect_area(rect))
            if best_score is None or score < best_score:
                best_score = score
                best_rect = fitz.Rect(rect) + (-grow, -grow, grow, grow)

        return best_rect, float(best_score[1]) if best_score is not None else float("inf")

    def _relative_rect_in_page(self, page_rect: fitz.Rect, rect: fitz.Rect) -> Tuple[float, float, float, float]:
        width = max(1.0, float(page_rect.width))
        height = max(1.0, float(page_rect.height))
        x0 = (float(rect.x0) - float(page_rect.x0)) / width
        y0 = (float(rect.y0) - float(page_rect.y0)) / height
        x1 = (float(rect.x1) - float(page_rect.x0)) / width
        y1 = (float(rect.y1) - float(page_rect.y0)) / height
        x0 = max(0.0, min(1.0, x0))
        y0 = max(0.0, min(1.0, y0))
        x1 = max(0.0, min(1.0, x1))
        y1 = max(0.0, min(1.0, y1))
        if x1 <= x0:
            x1 = min(1.0, x0 + 0.01)
        if y1 <= y0:
            y1 = min(1.0, y0 + 0.01)
        return (x0, y0, x1, y1)

    def _page_rect_from_relative_rect(self, page_rect: fitz.Rect, relative_rect: Tuple[float, float, float, float]) -> fitz.Rect:
        x0, y0, x1, y1 = relative_rect
        width = float(page_rect.width)
        height = float(page_rect.height)
        return fitz.Rect(
            float(page_rect.x0) + (x0 * width),
            float(page_rect.y0) + (y0 * height),
            float(page_rect.x0) + (x1 * width),
            float(page_rect.y0) + (y1 * height),
        )

    def _default_registration_mark_rect(self, click_point: fitz.Point) -> fitz.Rect:
        cx = self._coerce_float(getattr(click_point, "x", None))
        cy = self._coerce_float(getattr(click_point, "y", None))
        radius = 14.0
        return fitz.Rect(cx - radius, cy - radius, cx + radius, cy + radius)

    def _remove_registration_marks_in_relative_rect(
        self,
        relative_rect: Tuple[float, float, float, float],
        current_page: int,
        origin_label: str,
    ):
        self._push_undo_snapshot()

        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
        tmp_path = Path(tmp.name)
        tmp.close()
        self._register_temp_file(tmp_path)

        printer_mark_type = int(getattr(fitz, "PDF_ANNOT_PRINTER_MARK", -1))
        trap_net_type = int(getattr(fitz, "PDF_ANNOT_TRAP_NET", -1))
        removable_annot_types = {v for v in (printer_mark_type, trap_net_type) if v >= 0}

        removed_annots = 0
        processed_pages = 0

        try:
            src_doc = fitz.open(str(self.base_path))
            for page_idx in range(len(src_doc)):
                page = src_doc[page_idx]
                target_rect = self._page_rect_from_relative_rect(page.rect, relative_rect)
                if target_rect.is_empty:
                    continue

                annot = page.first_annot
                while annot:
                    try:
                        annot_type = int((annot.type or [None])[0])
                    except Exception:
                        annot_type = -1
                    try:
                        annot_rect = fitz.Rect(annot.rect)
                    except Exception:
                        annot_rect = fitz.Rect()
                    if annot_type in removable_annot_types and not annot_rect.is_empty and (
                        annot_rect.intersects(target_rect) or target_rect.contains(annot_rect.tl) or annot_rect.contains(target_rect.tl)
                    ):
                        annot = page.delete_annot(annot)
                        removed_annots += 1
                        continue
                    annot = annot.next

                page.add_redact_annot(target_rect, fill=(1, 1, 1), cross_out=False)
                page.apply_redactions(
                    images=getattr(fitz, "PDF_REDACT_IMAGE_NONE", 0),
                    graphics=getattr(fitz, "PDF_REDACT_LINE_ART_REMOVE_IF_COVERED", 1),
                    text=getattr(fitz, "PDF_REDACT_TEXT_NONE", 1),
                )
                processed_pages += 1

            src_doc.save(str(tmp_path), garbage=4, deflate=True)
            src_doc.close()
        except Exception as e:
            self._cleanup_temp_file(tmp_path)
            QMessageBox.critical(self, "인쇄 마크 제거 실패", f"오류:\n{e}")
            return

        old_base = self.base_path
        old_temp = self.temp_margin_file
        try:
            self._activate_temp_pdf(tmp_path, old_base, old_temp)
        except Exception as e:
            self._cleanup_temp_file(tmp_path)
            QMessageBox.critical(self, "열기 실패", f"임시 PDF 열기 실패:\n{e}")
            return

        self.current_page_index = min(current_page, len(self.doc) - 1)
        self._thumbnail_selected_pages = {self.current_page_index}
        self._clear_search_state()
        self._mark_modified()
        self._refresh_thumbnail_sidebar(force=True)
        self._scroll_to_current_after_render = self.continuous_view
        self.render_page()
        self.statusBar().showMessage(
            f"{origin_label} 기준 인쇄 마크 제거 완료: {processed_pages}쪽 적용, 주석 {removed_annots}개 제거",
            3500,
        )

    def remove_registration_marks_at_point(self, click_point: fitz.Point, page_index: int):
        if not self._has_open_doc():
            return

        page = self.doc[page_index]
        annot_rect, annot_dist_sq = self._find_registration_mark_annot_rect_near_point(page, click_point)
        vector_rect, vector_dist_sq = self._find_registration_mark_vector_rect_near_point(page, click_point)

        source_rect = None
        origin_label = "클릭 위치"
        if annot_rect is not None and (vector_rect is None or annot_dist_sq <= vector_dist_sq):
            source_rect = fitz.Rect(annot_rect)
            origin_label = "클릭한 주석 마크"
        elif vector_rect is not None:
            source_rect = fitz.Rect(vector_rect)
            origin_label = "클릭한 벡터 마크"
        else:
            source_rect = self._default_registration_mark_rect(click_point)

        source_rect &= page.rect
        relative_rect = self._relative_rect_in_page(page.rect, source_rect)
        page_count = len(self.doc)
        confirm = QMessageBox.question(
            self,
            "인쇄 마크 제거",
            f"{origin_label} 주변의 같은 위치를 {page_count}쪽 전체에서 제거합니다.\n\n"
            "원하지 않는 본문이 가까우면 더 정확히 그 마크를 다시 클릭하세요.\n"
            "계속할까요?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if confirm != QMessageBox.Yes:
            self.statusBar().showMessage("인쇄 마크 클릭 지정이 취소되었습니다.", 2500)
            return

        self._remove_registration_marks_in_relative_rect(relative_rect, page_index, origin_label)

    def _drawing_line_stats(self, items) -> Tuple[int, int, int, int]:
        line_count = 0
        horizontal_count = 0
        vertical_count = 0
        curve_count = 0
        for item in items or ():
            if not item:
                continue
            op = item[0]
            if op == "l" and len(item) >= 3:
                p1 = item[1]
                p2 = item[2]
                dx = abs(self._coerce_float(getattr(p2, "x", None)) - self._coerce_float(getattr(p1, "x", None)))
                dy = abs(self._coerce_float(getattr(p2, "y", None)) - self._coerce_float(getattr(p1, "y", None)))
                if dx < 0.4 and dy < 0.4:
                    continue
                line_count += 1
                if dy <= max(0.75, dx * 0.25):
                    horizontal_count += 1
                if dx <= max(0.75, dy * 0.25):
                    vertical_count += 1
            elif op in {"c", "v", "y"}:
                curve_count += 1
        return line_count, horizontal_count, vertical_count, curve_count

    def _target_like_drawing_clusters(self, page: fitz.Page) -> List[Dict[str, object]]:
        raw_candidates: List[Dict[str, object]] = []
        try:
            drawings = page.get_drawings()
        except Exception:
            return raw_candidates

        for path in drawings:
            try:
                rect = fitz.Rect(path.get("rect"))
            except Exception:
                continue
            if rect.is_empty:
                continue

            width = float(rect.width)
            height = float(rect.height)
            if width < 4.0 or height < 4.0 or width > 42.0 or height > 42.0:
                continue
            ratio = max(width / max(0.1, height), height / max(0.1, width))
            if ratio > 2.5:
                continue

            line_count, horizontal_count, vertical_count, curve_count = self._drawing_line_stats(path.get("items"))
            if horizontal_count < 1 or vertical_count < 1:
                continue

            merge_pad = max(3.0, self._coerce_float(path.get("width"), 1.0) * 2.0)
            raw_candidates.append(
                {
                    "rect": fitz.Rect(rect),
                    "touch_rect": fitz.Rect(rect) + (-merge_pad, -merge_pad, merge_pad, merge_pad),
                    "line_count": line_count,
                    "horizontal_count": horizontal_count,
                    "vertical_count": vertical_count,
                    "curve_count": curve_count,
                }
            )

        clusters: List[Dict[str, object]] = []
        for info in raw_candidates:
            info_rect = fitz.Rect(info["rect"])
            touch_rect = fitz.Rect(info["touch_rect"])
            matched = None
            for cluster in clusters:
                if touch_rect.intersects(cluster["touch_rect"]):
                    matched = cluster
                    break
            if matched is None:
                clusters.append(
                    {
                        "rect": fitz.Rect(info_rect),
                        "touch_rect": fitz.Rect(touch_rect),
                        "line_count": int(info["line_count"]),
                        "horizontal_count": int(info["horizontal_count"]),
                        "vertical_count": int(info["vertical_count"]),
                        "curve_count": int(info["curve_count"]),
                        "path_count": 1,
                    }
                )
                continue

            matched["rect"] |= info_rect
            matched["touch_rect"] |= touch_rect
            matched["line_count"] = int(matched["line_count"]) + int(info["line_count"])
            matched["horizontal_count"] = int(matched["horizontal_count"]) + int(info["horizontal_count"])
            matched["vertical_count"] = int(matched["vertical_count"]) + int(info["vertical_count"])
            matched["curve_count"] = int(matched["curve_count"]) + int(info["curve_count"])
            matched["path_count"] = int(matched["path_count"]) + 1

        filtered: List[Dict[str, object]] = []
        for cluster in clusters:
            rect = fitz.Rect(cluster["rect"])
            width = float(rect.width)
            height = float(rect.height)
            if width < 4.0 or height < 4.0 or width > 56.0 or height > 56.0:
                continue
            ratio = max(width / max(0.1, height), height / max(0.1, width))
            if ratio > 2.8:
                continue
            if int(cluster["horizontal_count"]) < 1 or int(cluster["vertical_count"]) < 1:
                continue
            filtered.append(cluster)
        return filtered

    def handle_thumbnail_reorder(self):
        if self._updating_thumbnail_list or not self._has_open_doc():
            return

        new_order = [
            self.thumbnail_list.item(row).data(Qt.UserRole)
            for row in range(self.thumbnail_list.count())
        ]
        if new_order == list(range(len(new_order))):
            return

        self._push_undo_snapshot()

        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
        tmp_path = Path(tmp.name)
        tmp.close()
        self._register_temp_file(tmp_path)

        try:
            src_doc = fitz.open(str(self.base_path))
            new_doc = fitz.open()
            for old_page_idx in new_order:
                new_doc.insert_pdf(src_doc, from_page=old_page_idx, to_page=old_page_idx)
            new_doc.save(str(tmp_path))
            new_doc.close()
            src_doc.close()
        except Exception as e:
            self._cleanup_temp_file(tmp_path)
            QMessageBox.critical(self, "페이지 순서 변경 실패", f"오류:\n{e}")
            self._refresh_thumbnail_sidebar(force=True)
            return

        old_base = self.base_path
        old_temp = self.temp_margin_file
        try:
            self._activate_temp_pdf(tmp_path, old_base, old_temp)
        except Exception as e:
            self._cleanup_temp_file(tmp_path)
            QMessageBox.critical(self, "열기 실패", f"임시 PDF 열기 실패:\n{e}")
            self._refresh_thumbnail_sidebar(force=True)
            return

        old_to_new = {old_idx: new_idx for new_idx, old_idx in enumerate(new_order)}
        self._rebuild_pending_changes_after_reorder(old_to_new)
        self.current_page_index = old_to_new.get(self.current_page_index, 0)
        self._thumbnail_selected_pages = {old_to_new[idx] for idx in self._thumbnail_selected_pages if idx in old_to_new}
        if not self._thumbnail_selected_pages:
            self._thumbnail_selected_pages = {self.current_page_index}
        self._clear_search_state()
        self._mark_modified()
        self._refresh_thumbnail_sidebar(force=True)
        self._scroll_to_current_after_render = self.continuous_view
        self.render_page()
        self.statusBar().showMessage("페이지 순서를 변경했습니다.", 2500)

    # ---------------- Margin Dialog ----------------

    def open_margin_dialog(self):
        if not self._has_open_doc():
            QMessageBox.warning(self, "알림", "먼저 PDF 파일을 열어주세요.")
            return

        dialog = QDialog(self)
        dialog.setWindowTitle("여백 / 크기 조정")
        dialog.setFixedSize(380, 420)

        def row(label, default):
            h = QHBoxLayout()
            h.addWidget(QLabel(label))
            le = QLineEdit(str(default))
            le.setFixedWidth(80)
            h.addStretch(1)
            h.addWidget(le)
            return h, le

        layout = QVBoxLayout(dialog)

        page = self.doc[self.current_page_index]
        rect = page.rect
        width_mm = round(rect.width * 25.4 / 72, 2)
        height_mm = round(rect.height * 25.4 / 72, 2)

        r1, w_edit = row("페이지 가로 (mm)", width_mm)
        r2, h_edit = row("페이지 세로 (mm)", height_mm)
        r3, cl_edit = row("왼쪽 크롭 (mm)", 0)
        r4, cr_edit = row("오른쪽 크롭 (mm)", 0)
        r5, ct_edit = row("위 크롭 (mm)", 0)
        r6, cb_edit = row("아래 크롭 (mm)", 0)
        r7, al_edit = row("왼쪽 여백 추가 (mm)", 0)
        r8, ar_edit = row("오른쪽 여백 추가 (mm)", 0)
        r9, at_edit = row("위 여백 추가 (mm)", 0)
        r10, ab_edit = row("아래 여백 추가 (mm)", 0)

        for r in (r1, r2, r3, r4, r5, r6):
            layout.addLayout(r)

        layout.addSpacing(4)

        for r in (r7, r8, r9, r10):
            layout.addLayout(r)

        btns = QHBoxLayout()
        apply_btn = QPushButton("적용")
        cancel_btn = QPushButton("취소")
        btns.addStretch(1)
        btns.addWidget(apply_btn)
        btns.addWidget(cancel_btn)

        layout.addStretch(1)
        layout.addLayout(btns)

        def apply():
            try:
                w = float(w_edit.text())
                h = float(h_edit.text())
                cl = float(cl_edit.text())
                cr = float(cr_edit.text())
                ct = float(ct_edit.text())
                cb = float(cb_edit.text())
                al = float(al_edit.text())
                ar = float(ar_edit.text())
                at = float(at_edit.text())
                ab = float(ab_edit.text())
            except Exception:
                QMessageBox.warning(dialog, "입력 오류", "숫자를 정확히 입력하세요.")
                return

            self._push_undo_snapshot()
            old_temp = self.temp_margin_file

            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
            tmp_path = Path(tmp.name)
            tmp.close()
            self._register_temp_file(tmp_path)

            try:
                process_margin(
                    input_pdf_path=self.base_path,
                    width_mm=w,
                    height_mm=h,
                    cl_mm=cl,
                    cr_mm=cr,
                    ct_mm=ct,
                    cb_mm=cb,
                    al_mm=al,
                    ar_mm=ar,
                    at_mm=at,
                    ab_mm=ab,
                    output_path=tmp_path
                )
            except Exception as e:
                QMessageBox.critical(dialog, "여백 조정 실패", str(e))
                self._cleanup_temp_file(tmp_path)
                return

            try:
                self._activate_temp_pdf(tmp_path, self.base_path, old_temp)
            except Exception as e:
                QMessageBox.critical(dialog, "열기 실패", f"임시 PDF 열기 실패:\n{e}")
                return

            self.current_page_index = min(self.current_page_index, len(self.doc) - 1)
            self._thumbnail_selected_pages = {self.current_page_index}
            self._clear_search_state()

            self._mark_modified()
            self._refresh_thumbnail_sidebar(force=True)
            self.render_page()
            dialog.accept()

        apply_btn.clicked.connect(apply)
        cancel_btn.clicked.connect(dialog.reject)

        dialog.exec()

    # ---------------- Help ----------------

    def show_help_dialog(self):
        msg = (
            "[단축키]\n"
            "- Cmd+F : 텍스트 찾기\n"
            "- Cmd+Shift+L : 자동 링크(추가/삭제)\n"
            "- Cmd+Backspace : 페이지 삭제\n"
            "- Cmd+Shift+X / C / V / D : 페이지 잘라내기 / 복사 / 붙여넣기 / 복제\n"
            "- Cmd+Shift+R : 선택 페이지 시계 방향 90도 회전\n"
            "- Cmd+Shift+P : 연속 페이지 보기 전환\n"
            "- 편집 > 인쇄 마크 제거 (클릭 지정) : 지울 마크를 한 번 클릭해 같은 위치를 전체 페이지에서 제거\n"
            "- 단어 클릭 : 텍스트 한 조각(단어/스팬) 수정\n"
            "- Shift+클릭 : 해당 줄 전체 수정\n"
            "- 우클릭 : 해당 단어에 링크 추가\n"
            "- Cmd+클릭 : 기존 링크 수정/제거\n"
            "- Cmd+우클릭 : 기존 링크 제거\n"
            "- Cmd+S : 다른 이름으로 저장\n"
            "- Cmd+W : 닫기\n"
            "- Cmd+= / - : 확대 / 축소\n\n"
            "[텍스트 수정]\n"
            "- 새 텍스트 폭이 선택 영역보다 길면 적용하지 않습니다.\n"
            "- Shift+클릭(줄 전체 수정)은 오른쪽으로 넘치는 텍스트를 허용합니다.\n"
            "- 자동 폰트는 원본 계열과 문자셋을 보고 설치된 한글/영문 폰트 중 가장 맞는 폰트를 우선 사용합니다.\n"
            "- 편집 창에서 설치된 시스템 폰트를 직접 골라 적용할 수 있습니다.\n"
            "- 아래/위첨자: x_{1}, x^{2}, x_{i}^{2} 형식을 지원합니다(선택 영역 내에서만 배치).\n"
            "- \\alpha, \\beta, \\Delta, \\to\\infty, >=, <= 등 수식/그리스 기호 입력을 지원합니다.\n\n"
            "[자동 링크]\n"
            "- 기본: equation 7.1 / figure 1.1 / table 1.1\n"
            "- 범위: equation 7.7 ~ 7.10, figure 1.1 ~ 1.4, table 1.1 ~ 1.4\n"
            "- 대소문자 무시: Equation / EQUATION 모두 인식\n"
            "- equation/figure/table 1.1은 1.10/1.12 같은 더 긴 번호에 적용되지 않도록 차단\n"
            "- 삭제 기능: 자동 링크에서 '링크 삭제' 선택 → 해당 단어(범위)에 걸린 링크 삭제 예약(저장 시 반영)\n"
            "- equation 1.1 → equations 1.1, eq. 1.1도 함께 처리\n"
            "- figure 1.1 → figures 1.1, fig. 1.1도 함께 처리\n"
            "- table 1.1 → tables 1.1, tbl. 1.1도 함께 처리\n"
            "- 검색도 equation/figure/table에 대해 같은 확장/충돌 방지 규칙을 사용합니다.\n"
            "- 찾기로 강조된 영역 위에서 우클릭하면 정확히 그 강조 영역에 링크를 추가합니다.\n"
        )
        QMessageBox.information(self, "도움말", msg)

    def show_formula_help_dialog(self):
        msg = (
            "[입력 형식]\n"
            "- 아래첨자: x_{1}, a_{5}, v_{avg}\n"
            "- 위첨자: x^{2}, r^{n}, y^{2}\n"
            "- 혼합: x_{i}^{2}, a_{n+1}^{k}\n"
            "- 줄 전체 수정(Shift+클릭)은 오른쪽으로 넘치는 입력을 허용합니다.\n"
            "- 단어 클릭 수정은 선택 폭보다 긴 텍스트를 허용하지 않습니다.\n\n"
            "[그리스 문자]\n"
            "- \\alpha \\beta \\gamma \\delta \\epsilon \\theta \\lambda \\mu \\pi \\sigma \\phi \\omega\n"
            "- \\Gamma \\Delta \\Theta \\Lambda \\Pi \\Sigma \\Phi \\Omega\n"
            "- 직접 입력: α β γ Δ Ω 같은 유니코드도 지원합니다.\n\n"
            "[관계 / 집합 기호]\n"
            "- \\in \\ni \\owns \\notin\n"
            "- \\subset \\subseteq \\subsetneq \\supset \\supseteq \\supsetneq\n"
            "- \\cup \\cap \\setminus \\emptyset\n"
            "- \\forall \\exists \\nexists\n"
            "- 직접 입력: ∈ ∉ ⊂ ⊆ ⊊ ⊃ ⊇ ∪ ∩ ∅ ∀ ∃ 도 지원합니다.\n\n"
            "[비교 / 화살표]\n"
            "- >= <= != -> <- => <=>\n"
            "- \\to \\mapsto \\leftarrow \\Rightarrow \\Leftrightarrow\n"
            "- 직접 입력: → ← ⇒ ⇔ ≥ ≤ ≠ 도 지원합니다.\n\n"
            "[연산 / 수식 기호]\n"
            "- \\pm \\mp \\times \\cdot \\div \\ast\n"
            "- \\oplus \\otimes \\ominus \\oslash\n"
            "- \\wedge \\vee \\land \\lor \\neg\n"
            "- \\angle \\perp \\parallel \\mid \\nmid\n"
            "- \\sim \\simeq \\cong\n"
            "- 직접 입력: ± × · ÷ ⊕ ⊗ ∠ ⟂ ∥ ∣ ∤ ∼ ≃ ≅ 도 지원합니다.\n\n"
            "[미적분 / 합 / 극한]\n"
            "- \\sum \\prod \\int \\iint \\iiint \\oint\n"
            "- \\partial \\nabla \\infty \\sqrt\n"
            "- \\to\\infty 형태 입력 가능\n"
            "- 직접 입력: ∑ ∏ ∫ ∂ ∇ ∞ √ 도 지원합니다.\n\n"
            "[점 / 생략 / 특수 기호]\n"
            "- \\ldots \\cdots \\vdots \\ddots\n"
            "- \\therefore \\because\n"
            "- \\aleph \\Re \\Im \\wp \\hbar \\ell\n"
            "- 직접 입력: … ⋯ ⋮ ⋱ ∴ ∵ ℵ ℜ ℑ ℘ ħ ℓ 도 지원합니다.\n\n"
            "[서식 옵션]\n"
            "- 폰트: 자동 또는 설치된 한글/영문 시스템 폰트 선택\n"
            "- 기울임(Italic): 본문/수식 기울임 적용\n"
            "- 굵게(Bold): 굵은 폰트 우선 적용\n"
            "- 위에 벡터 화살표: 선택 영역 상단 내부에 벡터 화살표 추가\n"
            "- 기본 높이: 위첨자 0.40em / 아래첨자 0.16em / 벡터 0.80em\n"
        )
        QMessageBox.information(self, "수식 도움말", msg)

    def show_developer_info_dialog(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("개발자 정보")
        dialog.setMinimumWidth(420)

        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        label = QLabel(
            '개발자의 유튜브 채널 : '
            '<a href="https://www.youtube.com/@univtutor">유니브튜터</a>'
        )
        label.setWordWrap(True)
        label.setOpenExternalLinks(True)
        label.setTextInteractionFlags(Qt.TextBrowserInteraction)
        layout.addWidget(label)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        ok_btn = QPushButton("확인")
        ok_btn.clicked.connect(dialog.accept)
        btn_row.addWidget(ok_btn)
        layout.addLayout(btn_row)

        dialog.exec()


# =========================
# Entry point helper
# =========================

def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Good PDF")
    app.setApplicationDisplayName("Good PDF")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
