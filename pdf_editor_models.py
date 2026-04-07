from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import List

import fitz  # PyMuPDF


# =========================
# Data structures
# =========================


@dataclass
class CharInfo:
    char: str
    rect: fitz.Rect
    raw_rect: fitz.Rect
    origin: fitz.Point


@dataclass
class SpanInfo:
    page_index: int
    line_index: int
    span_index: int
    rect: fitz.Rect
    raw_rect: fitz.Rect
    text: str
    origin: fitz.Point
    chars: List[CharInfo]
    font_name: str
    font_size: float
    color: int
    ascender: float = 0.0
    descender: float = 0.0


@dataclass
class LinkEditEntry:
    page_index: int
    link_rect: fitz.Rect
    new_page: int


@dataclass
class NewLinkEntry:
    page_index: int
    rect: fitz.Rect
    target_page: int


@dataclass
class LinkDeleteEntry:
    page_index: int
    link_rect: fitz.Rect


@dataclass
class PageClipboard:
    pdf_path: Path
    source_page_indices: List[int]
    link_edits: List[LinkEditEntry]
    new_links: List[NewLinkEntry]
    link_deletes: List[LinkDeleteEntry]
    cut_mode: bool


# =========================
# Margin helpers (PyMuPDF)
# =========================

MM_TO_PT = Decimal(72) / Decimal(25.4)


def mm_to_pt(mm) -> Decimal:
    return Decimal(str(mm)) * MM_TO_PT


def _rect_to_decimals(rect: fitz.Rect):
    return tuple(Decimal(str(value)) for value in (rect.x0, rect.y0, rect.x1, rect.y1))


def _absolute_cropbox(page: fitz.Page):
    cropbox = page.cropbox
    mediabox = page.mediabox
    if cropbox is None:
        return mediabox
    return fitz.Rect(
        cropbox.x0,
        mediabox.y1 - cropbox.y1,
        cropbox.x1,
        mediabox.y1 - cropbox.y0,
    )


def _pagebox_input_rect(mediabox: fitz.Rect, absolute_rect: fitz.Rect):
    return fitz.Rect(
        absolute_rect.x0,
        mediabox.y1 - absolute_rect.y1,
        absolute_rect.x1,
        mediabox.y1 - absolute_rect.y0,
    )


def adjust_page(page: fitz.Page, new_w_pt: Decimal, new_h_pt: Decimal,
                cl_pt: Decimal, cr_pt: Decimal, ct_pt: Decimal, cb_pt: Decimal,
                al_pt: Decimal = Decimal(0), ar_pt: Decimal = Decimal(0),
                at_pt: Decimal = Decimal(0), ab_pt: Decimal = Decimal(0)):
    # 기준 중심은 현재 보이는 페이지 영역(CropBox)을 우선 사용한다.
    # 그래야 연속 여백 조정 시 직전 조정 결과의 중심을 그대로 이어받는다.
    current_box = _absolute_cropbox(page) or page.mediabox
    llx, lly, urx, ury = _rect_to_decimals(current_box)
    cx, cy = (llx + urx) / 2, (lly + ury) / 2

    base_box = [
        cx - new_w_pt / 2,
        cy - new_h_pt / 2,
        cx + new_w_pt / 2,
        cy + new_h_pt / 2,
    ]

    media_box = [
        base_box[0] - al_pt,
        base_box[1] - ab_pt,
        base_box[2] + ar_pt,
        base_box[3] + at_pt,
    ]
    crop_box = [
        media_box[0] + cl_pt,
        media_box[1] + cb_pt,
        media_box[2] - cr_pt,
        media_box[3] - ct_pt,
    ]

    if crop_box[0] >= crop_box[2] or crop_box[1] >= crop_box[3]:
        raise ValueError("크롭/여백 추가 결과가 유효하지 않습니다. 값들을 다시 확인하세요.")

    media_rect = fitz.Rect(*(float(value) for value in media_box))

    page.set_mediabox(media_rect)
    actual_mediabox = page.mediabox
    crop_absolute_rect = fitz.Rect(
        float(actual_mediabox.x0) + float(cl_pt),
        float(actual_mediabox.y0) + float(cb_pt),
        float(actual_mediabox.x1) - float(cr_pt),
        float(actual_mediabox.y1) - float(ct_pt),
    )
    crop_input_rect = _pagebox_input_rect(actual_mediabox, crop_absolute_rect)

    page.set_cropbox(crop_input_rect)

    for setter_name in ("set_trimbox", "set_bleedbox", "set_artbox"):
        setter = getattr(page, setter_name, None)
        if setter is None:
            continue
        try:
            setter(crop_input_rect)
        except Exception:
            pass


def process_margin(input_pdf_path: Path, width_mm: float, height_mm: float,
                   cl_mm: float, cr_mm: float, ct_mm: float, cb_mm: float,
                   output_path: Path,
                   al_mm: float = 0, ar_mm: float = 0, at_mm: float = 0, ab_mm: float = 0):
    w_pt, h_pt = mm_to_pt(width_mm), mm_to_pt(height_mm)
    cl_pt, cr_pt = mm_to_pt(cl_mm), mm_to_pt(cr_mm)
    ct_pt, cb_pt = mm_to_pt(ct_mm), mm_to_pt(cb_mm)
    al_pt, ar_pt = mm_to_pt(al_mm), mm_to_pt(ar_mm)
    at_pt, ab_pt = mm_to_pt(at_mm), mm_to_pt(ab_mm)

    with fitz.open(str(input_pdf_path)) as pdf:
        for page in pdf:
            adjust_page(page, w_pt, h_pt, cl_pt, cr_pt, ct_pt, cb_pt, al_pt, ar_pt, at_pt, ab_pt)
        pdf.save(str(output_path), garbage=3, deflate=True)
