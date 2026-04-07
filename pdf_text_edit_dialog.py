from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
)

if TYPE_CHECKING:
    from pdf_text_edit_support import SimpleTextTarget


def run_text_edit_dialog(
    parent,
    target: "SimpleTextTarget",
    select_line: bool,
    font_options: Optional[List[Tuple[str, str]]] = None,
) -> Optional[Dict[str, Any]]:
    dialog = QDialog(parent)
    dialog.setWindowTitle("텍스트 수정 - 좌클릭 또는 Shift+좌클릭")
    dialog.resize(860, 430 if select_line else 400)
    dialog.setMinimumSize(780, 380)

    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(16, 14, 16, 14)
    layout.setSpacing(10)

    preview = target.text.replace("\n", " ")
    layout.addWidget(QLabel(f"선택된 텍스트: {preview}"))
    layout.addWidget(QLabel(f"원본 폰트: {target.font_name or '알 수 없음'}"))

    text_edit = QTextEdit()
    text_edit.setAcceptRichText(False)
    text_edit.setPlainText(target.text)
    text_edit.setMinimumHeight(160)
    layout.addWidget(text_edit)

    font_grid = QGridLayout()
    font_grid.setContentsMargins(0, 0, 0, 0)
    font_grid.setHorizontalSpacing(12)
    font_grid.setVerticalSpacing(8)

    all_font_options = list(font_options or [("__auto__", "자동 (원본 스타일 + 문자셋 기준)")])
    font_filter = QLineEdit()
    font_filter.setPlaceholderText("폰트 필터...")
    font_filter.setClearButtonEnabled(True)

    font_combo = QComboBox()
    font_combo.setMaxVisibleItems(24)
    font_combo.setMinimumContentsLength(30)
    font_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
    font_combo.setToolTip("자동 또는 설치된 한글/영문 시스템 폰트를 선택합니다.")

    def refresh_font_combo(query: str = ""):
        current_key = str(font_combo.currentData() or "__auto__")
        needle = (query or "").strip().lower()
        font_combo.blockSignals(True)
        font_combo.clear()
        for key, label in all_font_options:
            if needle and needle not in label.lower() and needle not in key.lower():
                continue
            font_combo.addItem(label, key)
        if font_combo.count() == 0:
            font_combo.addItem("검색 결과 없음", "__auto__")
        match_index = font_combo.findData(current_key)
        if match_index < 0:
            match_index = font_combo.findData("__auto__")
        if match_index >= 0:
            font_combo.setCurrentIndex(match_index)
        font_combo.blockSignals(False)

    refresh_font_combo()
    font_filter.textChanged.connect(refresh_font_combo)

    font_grid.addWidget(QLabel("적용 폰트"), 0, 0)
    font_grid.addWidget(font_combo, 0, 1, 1, 3)
    font_grid.addWidget(QLabel("필터"), 1, 0)
    font_grid.addWidget(font_filter, 1, 1, 1, 3)
    layout.addLayout(font_grid)

    option_row = QHBoxLayout()
    option_row.setContentsMargins(0, 0, 0, 0)
    option_row.setSpacing(22)

    italic_checkbox = QCheckBox("기울임(Italic)")
    italic_checkbox.setChecked(bool(target.is_italic))
    bold_checkbox = QCheckBox("굵게 (Bold)")
    bold_checkbox.setChecked(bool(target.is_bold))
    vector_checkbox = QCheckBox("위에 벡터 화살표(->) 그리기")
    vector_checkbox.setChecked(False)

    option_row.addWidget(italic_checkbox)
    option_row.addWidget(bold_checkbox)
    option_row.addWidget(vector_checkbox)
    option_row.addStretch(1)
    layout.addLayout(option_row)

    adjust_grid = QGridLayout()
    adjust_grid.setContentsMargins(0, 0, 0, 0)
    adjust_grid.setHorizontalSpacing(12)
    adjust_grid.setVerticalSpacing(8)

    super_spin = QDoubleSpinBox()
    super_spin.setRange(0.30, 1.20)
    super_spin.setSingleStep(0.02)
    super_spin.setDecimals(2)
    super_spin.setValue(0.40)
    super_spin.setSuffix(" em")

    sub_spin = QDoubleSpinBox()
    sub_spin.setRange(0.05, 0.60)
    sub_spin.setSingleStep(0.02)
    sub_spin.setDecimals(2)
    sub_spin.setValue(0.16)
    sub_spin.setSuffix(" em")

    vector_spin = QDoubleSpinBox()
    vector_spin.setRange(0.40, 1.20)
    vector_spin.setSingleStep(0.02)
    vector_spin.setDecimals(2)
    vector_spin.setValue(0.80)
    vector_spin.setSuffix(" em")

    adjust_grid.addWidget(QLabel("위첨자 높이"), 0, 0)
    adjust_grid.addWidget(super_spin, 0, 1)
    adjust_grid.addWidget(QLabel("아래첨자 높이"), 0, 2)
    adjust_grid.addWidget(sub_spin, 0, 3)
    adjust_grid.addWidget(QLabel("벡터 높이"), 1, 0)
    adjust_grid.addWidget(vector_spin, 1, 1)
    layout.addLayout(adjust_grid)

    layout.addSpacing(14)
    layout.addWidget(QLabel("팁: 아래/위첨자 입력은 x_{1}, x^{2}, x_{i}^{2} 형식을 사용하세요."))

    btns = QHBoxLayout()
    btns.setContentsMargins(0, 0, 0, 0)
    btns.setSpacing(10)
    apply_btn = QPushButton("적용")
    apply_btn.setFocusPolicy(Qt.NoFocus)
    cancel_btn = QPushButton("취소")
    cancel_btn.setFocusPolicy(Qt.NoFocus)
    btns.addStretch(1)
    btns.addWidget(apply_btn)
    btns.addWidget(cancel_btn)
    layout.addLayout(btns)

    action = {
        "text": None,
        "force_italic": bool(target.is_italic),
        "force_bold": bool(target.is_bold),
        "draw_vector_arrow": False,
        "super_shift_em": 0.40,
        "sub_shift_em": 0.16,
        "vector_shift_em": 0.80,
        "font_choice": "__auto__",
    }

    apply_pending = {"busy": False}

    def commit_pending_text_input():
        try:
            text_edit.setFocus(Qt.OtherFocusReason)
        except Exception:
            pass
        try:
            input_method = QGuiApplication.inputMethod()
            if input_method is not None:
                input_method.commit()
        except Exception:
            pass

    def finish_apply():
        action["text"] = text_edit.toPlainText()
        action["force_italic"] = italic_checkbox.isChecked()
        action["force_bold"] = bold_checkbox.isChecked()
        action["draw_vector_arrow"] = vector_checkbox.isChecked()
        action["super_shift_em"] = float(super_spin.value())
        action["sub_shift_em"] = float(sub_spin.value())
        action["vector_shift_em"] = float(vector_spin.value())
        action["font_choice"] = str(font_combo.currentData() or "__auto__")
        dialog.accept()

    def on_apply():
        if apply_pending["busy"]:
            return
        apply_pending["busy"] = True
        commit_pending_text_input()
        QTimer.singleShot(0, finish_apply)

    apply_btn.clicked.connect(on_apply)
    cancel_btn.clicked.connect(dialog.reject)

    if dialog.exec() != QDialog.Accepted:
        return None

    if action["text"] is None:
        return None
    return action
