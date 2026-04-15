"""텍스트 수정 다이얼로그 — 다크 모드 재디자인.

레이아웃 구조 (섹션 카드):
  - 헤더 카드: 선택 텍스트 / 원본 폰트 + 배지(기억/고정 적용 여부)
  - 텍스트 에디터
  - 섹션: 폰트  (콤보, 필터, pin 체크박스)
  - 섹션: 스타일 (italic / bold / 벡터 화살표)
  - 섹션: 높이 조정 (위/아래첨자, 벡터 em)
  - 팁 라인
  - 액션 버튼 (primary: 적용 / secondary: 취소)
"""

from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
)

if TYPE_CHECKING:
    from pdf_text_edit_support import SimpleTextTarget


_DIALOG_QSS = """
QDialog {
    background: #15171d;
    color: #e6e9ee;
}

/* 기본 라벨 */
QLabel {
    background: transparent;
    color: #d5dde8;
    font-size: 13px;
}

/* 헤더 카드 안의 작은 타이틀 "선택 텍스트", "원본 폰트" */
QLabel#InfoTitle {
    color: #7b8497;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.4px;
}
QLabel#InfoValue {
    color: #f5f7fa;
    font-size: 13px;
    font-weight: 600;
}

/* 섹션 헤더 (폰트 / 스타일 / 높이 조정) */
QLabel#SectionTitle {
    color: #8b95a7;
    font-size: 10px;
    font-weight: 800;
    letter-spacing: 1.8px;
    padding-left: 2px;
}

/* 각 행 라벨 */
QLabel#FieldLabel {
    color: #a4aebd;
    font-size: 12px;
    font-weight: 500;
}

/* 팁 한 줄 */
QLabel#HintLabel {
    color: #6b7280;
    font-size: 11px;
    padding: 2px 2px;
}

/* 현재 고정 폰트 안내 (pin 행 오른쪽) */
QLabel#PinStatusLabel {
    color: #7b8497;
    font-size: 11px;
    font-style: italic;
}

/* 배지 (기억된 폰트 / 고정 기본) */
QLabel#BadgeAlias {
    background: #1f3a2b;
    color: #86efac;
    font-size: 10px;
    font-weight: 800;
    padding: 2px 9px;
    border-radius: 8px;
    border: 1px solid #3b7f5d;
    letter-spacing: 0.3px;
}
QLabel#BadgePin {
    background: #1e2f4f;
    color: #93c5fd;
    font-size: 10px;
    font-weight: 800;
    padding: 2px 9px;
    border-radius: 8px;
    border: 1px solid #3b82f6;
    letter-spacing: 0.3px;
}

/* 카드 컨테이너 */
QFrame#HeaderPanel, QFrame#Section {
    background: #1e2027;
    border: 1px solid #2a2e38;
    border-radius: 12px;
}

/* 텍스트 에디터 */
QTextEdit {
    background: #0b0d12;
    color: #f5f7fa;
    border: 1px solid #2a2e38;
    border-radius: 12px;
    padding: 12px 14px;
    font-size: 15px;
    selection-background-color: #3b82f6;
    selection-color: #ffffff;
}
QTextEdit:focus {
    border: 1px solid #3b82f6;
}

/* 라인 에디트 (필터) */
QLineEdit {
    background: #14171e;
    color: #f0f3f7;
    border: 1px solid #2a2e38;
    border-radius: 8px;
    padding: 6px 10px;
    min-height: 26px;
    font-size: 12px;
    selection-background-color: #3b82f6;
    selection-color: #ffffff;
}
QLineEdit:focus {
    border-color: #3b82f6;
}

/* 콤보박스 */
QComboBox {
    background: #262a33;
    color: #f5f7fa;
    border: 1px solid #333945;
    border-radius: 8px;
    padding: 6px 12px;
    min-height: 28px;
    font-size: 12px;
    font-weight: 500;
}
QComboBox:hover {
    border-color: #4a5263;
}
QComboBox:focus {
    border-color: #3b82f6;
}
QComboBox::drop-down {
    width: 22px;
    border: none;
    subcontrol-origin: padding;
    subcontrol-position: center right;
}
QComboBox QAbstractItemView {
    background: #14171e;
    color: #f0f3f7;
    border: 1px solid #333945;
    selection-background-color: #3b82f6;
    selection-color: #ffffff;
    padding: 6px;
    outline: 0;
}

/* 스핀박스 */
QDoubleSpinBox, QSpinBox {
    background: #262a33;
    color: #f5f7fa;
    border: 1px solid #333945;
    border-radius: 8px;
    padding: 5px 10px;
    min-height: 28px;
    font-size: 12px;
    font-weight: 600;
    selection-background-color: #3b82f6;
    selection-color: #ffffff;
}
QDoubleSpinBox:hover, QSpinBox:hover {
    border-color: #4a5263;
}
QDoubleSpinBox:focus, QSpinBox:focus {
    border-color: #3b82f6;
}
QDoubleSpinBox::up-button, QSpinBox::up-button,
QDoubleSpinBox::down-button, QSpinBox::down-button {
    background: #333945;
    border: none;
    width: 18px;
}
QDoubleSpinBox::up-button:hover, QSpinBox::up-button:hover,
QDoubleSpinBox::down-button:hover, QSpinBox::down-button:hover {
    background: #4a5263;
}

/* 체크박스 — indicator 색 채움으로 체크 상태 표시 (이미지 없이) */
QCheckBox {
    color: #dfe5ef;
    spacing: 10px;
    font-size: 12px;
    font-weight: 500;
    padding: 4px 0;
    background: transparent;
}
QCheckBox:hover {
    color: #ffffff;
}
QCheckBox::indicator {
    width: 18px;
    height: 18px;
    border: 2px solid #4a5263;
    border-radius: 4px;
    background: #0b0d12;
}
QCheckBox::indicator:hover {
    border-color: #6b7280;
}
QCheckBox::indicator:checked {
    background: #3b82f6;
    border: 2px solid #60a5fa;
}
QCheckBox::indicator:checked:hover {
    background: #60a5fa;
    border-color: #93c5fd;
}
QCheckBox::indicator:disabled {
    background: #1d2028;
    border-color: #2a2e38;
}

/* 버튼 */
QPushButton {
    background: #262a33;
    color: #e6e9ee;
    border: 1px solid #333945;
    border-radius: 9px;
    padding: 9px 26px;
    min-height: 32px;
    font-size: 13px;
    font-weight: 600;
}
QPushButton:hover {
    background: #333945;
    border-color: #4a5263;
}
QPushButton:pressed {
    background: #1b1e26;
}

/* 1차 액션 버튼 (적용) */
QPushButton#PrimaryButton {
    background: #3b82f6;
    color: #ffffff;
    border: 1px solid #60a5fa;
}
QPushButton#PrimaryButton:hover {
    background: #60a5fa;
    border-color: #93c5fd;
}
QPushButton#PrimaryButton:pressed {
    background: #2563eb;
    border-color: #3b82f6;
}
"""


def _make_section(title: str) -> Tuple[QFrame, QVBoxLayout]:
    """섹션 카드 생성. (카드 프레임, 내용 레이아웃) 튜플 반환.

    카드의 수직 크기 정책을 Minimum 으로 고정해서 창이 작아져도 내용물의
    sizeHint 아래로 압축되지 않도록 한다 (이전 버전은 Preferred 라서 작은
    창에서 자식 위젯들이 overlap 됐다).
    """
    card = QFrame()
    card.setObjectName("Section")
    card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
    outer = QVBoxLayout(card)
    outer.setContentsMargins(16, 12, 16, 14)
    outer.setSpacing(10)

    title_label = QLabel(title.upper())
    title_label.setObjectName("SectionTitle")
    outer.addWidget(title_label)

    content = QVBoxLayout()
    content.setContentsMargins(0, 0, 0, 0)
    content.setSpacing(8)
    outer.addLayout(content)

    return card, content


def _make_field_label(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName("FieldLabel")
    lbl.setMinimumWidth(78)
    lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
    return lbl


def run_text_edit_dialog(
    parent,
    target: "SimpleTextTarget",
    select_line: bool,
    font_options: Optional[List[Tuple[str, str]]] = None,
    initial_font_choice: Optional[str] = None,
    pinned_font_choice: Optional[str] = None,
    alias_applied: bool = False,
) -> Optional[Dict[str, Any]]:
    dialog = QDialog(parent)
    dialog.setWindowTitle("텍스트 수정")
    # 모든 섹션(헤더/에디터/폰트/스타일/높이) 합산 필요 높이 ~700px.
    # 여유분 포함해서 기본 780, 최소 720.
    dialog.resize(940, 820 if select_line else 780)
    dialog.setMinimumSize(860, 720)
    dialog.setStyleSheet(_DIALOG_QSS)

    root = QVBoxLayout(dialog)
    root.setContentsMargins(20, 18, 20, 18)
    root.setSpacing(12)

    # ---------------- Header card ----------------
    header_card = QFrame()
    header_card.setObjectName("HeaderPanel")
    header_card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
    header_layout = QVBoxLayout(header_card)
    header_layout.setContentsMargins(16, 12, 16, 12)
    header_layout.setSpacing(6)

    # 선택 텍스트 라인
    sel_row = QHBoxLayout()
    sel_row.setSpacing(10)
    sel_title = QLabel("선택 텍스트")
    sel_title.setObjectName("InfoTitle")
    sel_title.setMinimumWidth(76)
    sel_preview = (target.text or "").replace("\n", " ")
    sel_value = QLabel(sel_preview)
    sel_value.setObjectName("InfoValue")
    sel_value.setTextInteractionFlags(Qt.TextSelectableByMouse)
    sel_row.addWidget(sel_title)
    sel_row.addWidget(sel_value, 1)
    header_layout.addLayout(sel_row)

    # 원본 폰트 라인
    orig_row = QHBoxLayout()
    orig_row.setSpacing(10)
    orig_title = QLabel("원본 폰트")
    orig_title.setObjectName("InfoTitle")
    orig_title.setMinimumWidth(76)
    orig_value = QLabel(target.font_name or "알 수 없음")
    orig_value.setObjectName("InfoValue")
    orig_value.setTextInteractionFlags(Qt.TextSelectableByMouse)
    orig_row.addWidget(orig_title)
    orig_row.addWidget(orig_value, 1)
    if alias_applied:
        badge = QLabel("이 PDF 기억 적용")
        badge.setObjectName("BadgeAlias")
        orig_row.addWidget(badge)
    elif pinned_font_choice and initial_font_choice == pinned_font_choice:
        badge = QLabel("고정 기본 적용")
        badge.setObjectName("BadgePin")
        orig_row.addWidget(badge)
    header_layout.addLayout(orig_row)

    root.addWidget(header_card)

    # ---------------- Text editor ----------------
    text_edit = QTextEdit()
    text_edit.setAcceptRichText(False)
    text_edit.setPlainText(target.text)
    text_edit.setMinimumHeight(140)
    text_edit.setMaximumHeight(240)
    # stretch factor 0 (= default) 을 유지해서 text_edit 이 다른 섹션을
    # eviction 하지 못하도록 한다. 대신 root 마지막에 stretch 를 둬서 남는
    # 세로 공간은 하단 여백으로 흡수한다.
    text_edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
    root.addWidget(text_edit)

    # ---------------- Font section ----------------
    font_section, font_content = _make_section("폰트")

    all_font_options = list(font_options or [("__auto__", "자동 (원본 스타일 + 문자셋 기준)")])
    available_keys = {key for key, _ in all_font_options}
    desired_initial_key = (
        initial_font_choice
        if initial_font_choice and initial_font_choice in available_keys
        else "__auto__"
    )

    font_combo = QComboBox()
    font_combo.setMaxVisibleItems(24)
    font_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
    font_combo.setToolTip(
        "자동 또는 설치된 한글/영문 시스템 폰트를 선택합니다.\n"
        "★ 표시는 최근 사용한 폰트입니다."
    )

    font_filter = QLineEdit()
    font_filter.setPlaceholderText("폰트 이름으로 필터...")
    font_filter.setClearButtonEnabled(True)

    def refresh_font_combo(query: str = ""):
        current_key = str(font_combo.currentData() or desired_initial_key)
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

    # 폰트 콤보 행
    combo_row = QHBoxLayout()
    combo_row.setSpacing(10)
    combo_row.addWidget(_make_field_label("적용 폰트"))
    combo_row.addWidget(font_combo, 1)
    font_content.addLayout(combo_row)

    # 필터 행
    filter_row = QHBoxLayout()
    filter_row.setSpacing(10)
    filter_row.addWidget(_make_field_label("검색"))
    filter_row.addWidget(font_filter, 1)
    font_content.addLayout(filter_row)

    # pin 체크박스 + 현재 고정 라벨
    pin_checkbox = QCheckBox("앞으로 이 폰트를 기본값으로 사용")
    pin_checkbox.setToolTip(
        "체크 후 [적용] 을 누르면 선택한 폰트가 다른 PDF 에서도 자동으로 먼저 선택됩니다.\n"
        "이미 고정된 폰트에서 체크를 해제하면 고정이 풀립니다."
    )
    pin_checkbox.setChecked(
        bool(pinned_font_choice)
        and desired_initial_key == pinned_font_choice
        and desired_initial_key != "__auto__"
    )

    pin_label = QLabel()
    pin_label.setObjectName("PinStatusLabel")

    def _pin_name_for(key: Optional[str]) -> str:
        if not key or key == "__auto__":
            return "없음"
        for k, label in all_font_options:
            if k == key:
                clean = label.lstrip("★ ").strip()
                bracket = clean.find(" [")
                return clean[:bracket] if bracket > 0 else clean
        try:
            return Path(key).stem
        except Exception:
            return str(key)

    pin_label.setText(f"현재 고정: {_pin_name_for(pinned_font_choice)}")

    pin_row = QHBoxLayout()
    pin_row.setSpacing(10)
    pin_row.addSpacing(88)  # field label 폭만큼 들여쓰기 (콤보/필드 열 기준선 정렬)
    pin_row.addWidget(pin_checkbox)
    pin_row.addStretch(1)
    pin_row.addWidget(pin_label)
    font_content.addLayout(pin_row)

    refresh_font_combo()
    font_filter.textChanged.connect(refresh_font_combo)

    def _on_combo_changed(_=0):
        # "__auto__" 로 변경 시 pin 은 의미 없으므로 자동 해제.
        # 그 외에는 사용자가 설정한 체크박스 상태 보존.
        if str(font_combo.currentData() or "__auto__") == "__auto__":
            pin_checkbox.setChecked(False)

    font_combo.currentIndexChanged.connect(_on_combo_changed)

    root.addWidget(font_section)

    # ---------------- Style section ----------------
    style_section, style_content = _make_section("스타일")

    italic_checkbox = QCheckBox("기울임 (Italic)")
    italic_checkbox.setChecked(bool(target.is_italic))
    bold_checkbox = QCheckBox("굵게 (Bold)")
    bold_checkbox.setChecked(bool(target.is_bold))
    vector_checkbox = QCheckBox("위에 벡터 화살표 (→) 그리기")
    vector_checkbox.setChecked(False)

    style_row = QHBoxLayout()
    style_row.setSpacing(28)
    style_row.addWidget(italic_checkbox)
    style_row.addWidget(bold_checkbox)
    style_row.addWidget(vector_checkbox)
    style_row.addStretch(1)
    style_content.addLayout(style_row)

    root.addWidget(style_section)

    # ---------------- Adjust section ----------------
    adjust_section, adjust_content = _make_section("높이 조정")

    super_spin = QDoubleSpinBox()
    super_spin.setRange(0.30, 1.20)
    super_spin.setSingleStep(0.02)
    super_spin.setDecimals(2)
    super_spin.setValue(0.40)
    super_spin.setSuffix(" em")
    super_spin.setMinimumWidth(120)

    sub_spin = QDoubleSpinBox()
    sub_spin.setRange(0.05, 0.60)
    sub_spin.setSingleStep(0.02)
    sub_spin.setDecimals(2)
    sub_spin.setValue(0.16)
    sub_spin.setSuffix(" em")
    sub_spin.setMinimumWidth(120)

    vector_spin = QDoubleSpinBox()
    vector_spin.setRange(0.40, 1.20)
    vector_spin.setSingleStep(0.02)
    vector_spin.setDecimals(2)
    vector_spin.setValue(0.80)
    vector_spin.setSuffix(" em")
    vector_spin.setMinimumWidth(120)

    def _adjust_field(label_text: str, spin: QDoubleSpinBox) -> QHBoxLayout:
        h = QHBoxLayout()
        h.setSpacing(8)
        lbl = QLabel(label_text)
        lbl.setObjectName("FieldLabel")
        lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        h.addWidget(lbl)
        h.addWidget(spin)
        return h

    # 세 항목을 한 줄에 나란히 배치
    adjust_row = QHBoxLayout()
    adjust_row.setSpacing(22)
    adjust_row.addLayout(_adjust_field("위첨자 높이", super_spin))
    adjust_row.addLayout(_adjust_field("아래첨자 높이", sub_spin))
    adjust_row.addLayout(_adjust_field("벡터 높이", vector_spin))
    adjust_row.addStretch(1)
    adjust_content.addLayout(adjust_row)

    root.addWidget(adjust_section)

    # ---------------- Tip line ----------------
    tip = QLabel("💡 위/아래첨자 입력:  x_{1},  x^{2},  x_{i}^{2}")
    tip.setObjectName("HintLabel")
    root.addWidget(tip)

    # 남은 세로 공간을 버튼 위쪽으로 흡수 → 위젯들이 압축되며 겹치는 현상 방지
    root.addStretch(1)

    # ---------------- Buttons ----------------
    btn_row = QHBoxLayout()
    btn_row.setContentsMargins(0, 4, 0, 0)
    btn_row.setSpacing(10)
    apply_btn = QPushButton("적용")
    apply_btn.setObjectName("PrimaryButton")
    apply_btn.setFocusPolicy(Qt.NoFocus)
    cancel_btn = QPushButton("취소")
    cancel_btn.setFocusPolicy(Qt.NoFocus)
    btn_row.addStretch(1)
    btn_row.addWidget(cancel_btn)
    btn_row.addWidget(apply_btn)
    root.addLayout(btn_row)

    # ---------------- Action wiring ----------------
    action: Dict[str, Any] = {
        "text": None,
        "force_italic": bool(target.is_italic),
        "force_bold": bool(target.is_bold),
        "draw_vector_arrow": False,
        "super_shift_em": 0.40,
        "sub_shift_em": 0.16,
        "vector_shift_em": 0.80,
        "font_choice": "__auto__",
        "pin_requested": False,
        "initial_font_choice": initial_font_choice or "__auto__",
    }

    apply_pending = {"busy": False}

    def commit_pending_text_input():
        try:
            text_edit.setFocus(Qt.OtherFocusReason)
        except Exception:
            pass
        try:
            im = QGuiApplication.inputMethod()
            if im is not None:
                im.commit()
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
        action["pin_requested"] = bool(pin_checkbox.isChecked())
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
