import re
import tempfile
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import fitz  # PyMuPDF
from PySide6.QtWidgets import QMessageBox

from pdf_editor_models import CharInfo, SpanInfo
from pdf_text_edit_dialog import run_text_edit_dialog


@dataclass
class SimpleTextTarget:
    page_index: int
    line_index: int
    rect: fitz.Rect
    text: str
    origin: fitz.Point
    font_name: str
    font_size: float
    color: int
    is_italic: bool = False
    is_bold: bool = False
    span_index: int = -1
    char_start: int = -1
    char_end: int = -1


@dataclass
class _LineRerenderPiece:
    text: str
    rect: fitz.Rect
    origin: fitz.Point
    font_name: str
    font_size: float
    color: int
    is_italic: bool = False
    is_bold: bool = False


@dataclass
class _SystemFontResource:
    path: Path
    display_name: str
    family_hint: str
    supports_ascii: bool
    supports_hangul: bool
    supports_math: bool
    is_bold: bool = False
    is_italic: bool = False


@dataclass
class _ResolvedInsertFont:
    font_name: str
    font_path: Optional[Path] = None
    font_buffer: Optional[bytes] = None
    measure_font_path: Optional[Path] = None
    measure_font_buffer: Optional[bytes] = None
    source_font_name: str = ""


class TextEditSupport:
    def __init__(self, window):
        self.window = window

    def _font_alias_from_path(self, prefix: str, path: Path, force_italic: bool = False, force_bold: bool = False) -> str:
        style = "n"
        if force_bold and force_italic:
            style = "bi"
        elif force_bold:
            style = "b"
        elif force_italic:
            style = "i"
        stem = re.sub(r"[^a-z0-9]+", "", path.stem.lower())[:20] or "font"
        return f"{prefix}_{style}_{stem}"

    def _font_alias_from_value(self, prefix: str, value: str, force_italic: bool = False, force_bold: bool = False) -> str:
        style = "n"
        if force_bold and force_italic:
            style = "bi"
        elif force_bold:
            style = "b"
        elif force_italic:
            style = "i"
        stem = re.sub(r"[^a-z0-9]+", "", (value or "").lower())[:20] or "font"
        return f"{prefix}_{style}_{stem}"

    def _normalized_symbol_font_name(self, font_name: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", (font_name or "").lower())

    def _symbol_font_codec(self, font_name: str) -> Tuple[Dict[str, str], Dict[str, str]]:
        normalized_font = self._normalized_symbol_font_name(font_name)

        if "mathematicalpi" in normalized_font:
            decode = {
                "5": "=",
                "2": "-",
            }
            encode = {
                "=": "5",
                "＝": "5",
                "-": "2",
                "−": "2",
                "–": "2",
                "—": "2",
            }
            return decode, encode

        return {}, {}

    def decode_symbol_font_char(self, font_name: str, char: str) -> str:
        if not char:
            return ""
        decode, _ = self._symbol_font_codec(font_name)
        return decode.get(char, char)

    def _encode_display_text_for_font(self, text: str, font_name: str) -> str:
        if not text:
            return text
        _, encode = self._symbol_font_codec(font_name)
        if not encode:
            return text
        return "".join(encode.get(ch, ch) for ch in text)

    def _text_for_resolved_plan(self, text: str, resolved_plan: Optional[_ResolvedInsertFont]) -> str:
        if resolved_plan is None:
            return text
        if resolved_plan.font_buffer is None and resolved_plan.measure_font_buffer is None:
            return text
        return self._encode_display_text_for_font(text, resolved_plan.source_font_name or resolved_plan.font_name)

    def _compact_vertical_bounds(
        self,
        baseline_y: float,
        font_size: float,
        ascender: float = 0.0,
        descender: float = 0.0,
    ) -> Tuple[float, float]:
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

    def _rect_distance_sq(self, rect: fitz.Rect, point: fitz.Point) -> float:
        dx = 0.0
        dy = 0.0
        if point.x < rect.x0:
            dx = float(rect.x0 - point.x)
        elif point.x > rect.x1:
            dx = float(point.x - rect.x1)
        if point.y < rect.y0:
            dy = float(rect.y0 - point.y)
        elif point.y > rect.y1:
            dy = float(point.y - rect.y1)
        return dx * dx + dy * dy

    def _is_token_char(self, c: str) -> bool:
        return bool(c) and (c.isalnum() or c in "_-'")

    def _char_gap_x(self, left_char: CharInfo, right_char: CharInfo) -> float:
        try:
            return float(right_char.rect.x0) - float(left_char.rect.x1)
        except Exception:
            return 0.0

    def _split_gap_threshold(self, chars: List[CharInfo], font_size: float) -> float:
        size = max(2.0, float(font_size or 11.0))
        positive_gaps: List[float] = []
        for idx in range(len(chars) - 1):
            left = chars[idx]
            right = chars[idx + 1]
            if not left.char.strip() or not right.char.strip():
                continue
            gap = self._char_gap_x(left, right)
            if gap > 0:
                positive_gaps.append(float(gap))

        if not positive_gaps:
            return max(0.65, size * 0.08)

        positive_gaps.sort()
        sample = positive_gaps[:max(1, (len(positive_gaps) * 2 + 2) // 3)]
        normal_gap = sample[len(sample) // 2]
        return max(0.65, size * 0.09, normal_gap * 1.85)

    def _chars_to_rerender_pieces(
        self,
        chars: List[CharInfo],
        baseline_y: float,
        font_name: str,
        font_size: float,
        color: int,
        is_italic: bool = False,
        is_bold: bool = False,
    ) -> List[_LineRerenderPiece]:
        filtered = [ch for ch in (chars or []) if ch.char]
        if not filtered:
            return []

        threshold = self._split_gap_threshold(filtered, font_size)
        pieces: List[_LineRerenderPiece] = []
        current: List[CharInfo] = []

        def flush():
            if not current:
                return
            text = "".join(ch.char for ch in current)
            if not text.strip():
                current.clear()
                return
            rect = self._union_char_rects(current)
            origin_x = next((float(ch.rect.x0) for ch in current if ch.char.strip()), float(current[0].rect.x0))
            pieces.append(
                _LineRerenderPiece(
                    text=text,
                    rect=rect,
                    origin=fitz.Point(origin_x, float(baseline_y)),
                    font_name=font_name or "Times-Roman",
                    font_size=float(font_size or 11.0),
                    color=int(color or 0),
                    is_italic=is_italic,
                    is_bold=is_bold,
                )
            )
            current.clear()

        prev_non_space = None
        for ch in filtered:
            if not ch.char.strip():
                flush()
                prev_non_space = None
                continue

            if prev_non_space is not None:
                gap = self._char_gap_x(prev_non_space, ch)
                if gap > threshold:
                    flush()

            current.append(ch)
            prev_non_space = ch

        flush()
        return pieces

    def _span_hit_score(self, span: SpanInfo, point: fitz.Point, hit_char_index: int = -1) -> Tuple[float, float, float, float]:
        rect = fitz.Rect(span.rect)
        baseline_y = float(span.origin.y)
        vertical_dist = abs(float(point.y) - baseline_y)
        center_y = (float(rect.y0) + float(rect.y1)) * 0.5
        center_x = (float(rect.x0) + float(rect.x1)) * 0.5
        x_dist = abs(float(point.x) - center_x)
        area = max(1e-6, float(rect.width * rect.height))

        if 0 <= hit_char_index < len(span.chars):
            char_rect = fitz.Rect(span.chars[hit_char_index].rect)
            center_y = (float(char_rect.y0) + float(char_rect.y1)) * 0.5
            center_x = (float(char_rect.x0) + float(char_rect.x1)) * 0.5
            x_dist = abs(float(point.x) - center_x)
            area = max(1e-6, float(char_rect.width * char_rect.height))

        return (vertical_dist, x_dist, area, abs(float(point.y) - center_y))

    def _make_span_target(self, span: SpanInfo) -> Optional[SimpleTextTarget]:
        if not span.text:
            return None
        font_name = span.font_name or "Times-Roman"
        return SimpleTextTarget(
            page_index=span.page_index,
            line_index=span.line_index,
            rect=fitz.Rect(span.rect),
            text=span.text,
            origin=fitz.Point(float(span.origin.x), float(span.origin.y)),
            font_name=font_name,
            font_size=float(span.font_size or 11.0),
            color=int(span.color or 0),
            is_italic=self._font_is_italic(font_name),
            is_bold=self._font_is_bold(font_name),
            span_index=int(span.span_index),
            char_start=0,
            char_end=max(-1, len(span.chars) - 1),
        )

    def _make_char_target(self, span: SpanInfo, ch: CharInfo, char_index: int = -1) -> SimpleTextTarget:
        font_name = span.font_name or "Times-Roman"
        return SimpleTextTarget(
            page_index=span.page_index,
            line_index=span.line_index,
            rect=fitz.Rect(ch.rect),
            text=ch.char,
            origin=fitz.Point(float(ch.rect.x0), float(span.origin.y)),
            font_name=font_name,
            font_size=float(span.font_size or 11.0),
            color=int(span.color or 0),
            is_italic=self._font_is_italic(font_name),
            is_bold=self._font_is_bold(font_name),
            span_index=int(span.span_index),
            char_start=int(char_index),
            char_end=int(char_index),
        )

    def effective_target_rect(self, target: SimpleTextTarget, allow_overflow_right: bool = False) -> fitz.Rect:
        return self._effective_edit_rect(target, allow_overflow_right)

    def _font_is_italic(self, font_name: str) -> bool:
        lowered = (font_name or "").lower()
        return ("italic" in lowered) or ("oblique" in lowered)

    def _font_is_bold(self, font_name: str) -> bool:
        lowered = (font_name or "").lower()
        return "bold" in lowered

    def _font_family_hint(self, font_name: str) -> str:
        lowered = (font_name or "").strip().lower()
        serif_keys = (
            "times", "roman", "cmr", "garamond", "minion", "palatino", "bookman", "serif", "stix", "termes"
        )
        sans_keys = (
            "helvetica", "arial", "sans", "gothic", "univers", "frutiger", "futura", "grotesk"
        )
        if lowered.startswith("ftnr") or any(k in lowered for k in serif_keys):
            return "times"
        if lowered.startswith("funi") or any(k in lowered for k in sans_keys):
            return "helvetica"
        return "times"

    def _normalize_font_lookup_key(self, value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", (value or "").lower())

    def _font_lookup_tokens(self, value: str) -> List[str]:
        expanded = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", value or "")
        expanded = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", expanded)
        tokens = re.findall(r"[a-z0-9]+", expanded.lower())
        stop_words = {
            "std", "roman", "regular", "normal", "bold", "italic", "oblique",
            "medium", "book", "text", "display", "caption", "semibold",
            "demibold", "demi", "light", "thin", "black", "heavy", "ultra",
            "extra", "condensed", "cond", "narrow", "extended", "mt", "ps",
            "psmt", "lt", "bt",
        }
        aliases = {
            "myeongjo": "myungjo",
        }
        out: List[str] = []
        seen = set()
        for token in tokens:
            token = aliases.get(token, token)
            if len(token) <= 1 or token in stop_words:
                continue
            if token in seen:
                continue
            seen.add(token)
            out.append(token)
        return out

    def _font_name_match_score(
        self,
        source_font_name: str,
        resource: _SystemFontResource,
        source_bold: bool = False,
        source_italic: bool = False,
    ) -> Tuple[int, int, int, int, int, int]:
        source_norm = self._normalize_font_lookup_key(source_font_name)
        source_tokens = set(self._font_lookup_tokens(source_font_name))
        if not source_norm and not source_tokens:
            return (3, 0, 0, 99, 99, 999)

        src_lower = (source_font_name or "").lower()
        src_bold = source_bold or any(k in src_lower for k in ("bold", "heavy", "black"))
        src_italic = source_italic or any(k in src_lower for k in ("italic", "oblique"))

        style_penalty = 0
        if src_bold != resource.is_bold:
            style_penalty += 1
        if src_italic != resource.is_italic:
            style_penalty += 1

        best = (3, 0, style_penalty, 99, 99, 999)
        for candidate in (resource.display_name, resource.path.stem):
            candidate_norm = self._normalize_font_lookup_key(candidate)
            candidate_tokens = set(self._font_lookup_tokens(candidate))
            shared = len(source_tokens & candidate_tokens)
            if source_norm and candidate_norm and source_norm == candidate_norm:
                relation = 0
            elif source_norm and candidate_norm and (candidate_norm in source_norm or source_norm in candidate_norm):
                relation = 1
            elif shared > 0:
                relation = 2
            else:
                relation = 3

            missing = max(0, len(source_tokens) - shared)
            extra = max(0, len(candidate_tokens) - shared)
            score = (relation, -shared, style_penalty, missing, extra, len(candidate_norm or candidate))
            if score < best:
                best = score
        return best

    def _font_name_pair_score(self, source_font_name: str, candidate_font_name: str) -> Tuple[int, int, int, int, int]:
        source_norm = self._normalize_font_lookup_key(source_font_name)
        candidate_norm = self._normalize_font_lookup_key(candidate_font_name)
        source_tokens = set(self._font_lookup_tokens(source_font_name))
        candidate_tokens = set(self._font_lookup_tokens(candidate_font_name))

        shared = len(source_tokens & candidate_tokens)
        if source_norm and candidate_norm and source_norm == candidate_norm:
            relation = 0
        elif source_norm and candidate_norm and (candidate_norm in source_norm or source_norm in candidate_norm):
            relation = 1
        elif shared > 0:
            relation = 2
        else:
            relation = 3

        missing = max(0, len(source_tokens) - shared)
        extra = max(0, len(candidate_tokens) - shared)
        return (relation, -shared, missing, extra, len(candidate_norm or candidate_font_name))

    def _strip_pdf_subset_prefix(self, font_name: str) -> str:
        return re.sub(r"^[A-Z]{6}\+", "", (font_name or "").strip())

    def _is_builtin_insert_font_name(self, font_name: str) -> bool:
        lowered = (font_name or "").strip().lower()
        base14 = {
            "courier", "courier-bold", "courier-oblique", "courier-boldoblique",
            "helvetica", "helvetica-bold", "helvetica-oblique", "helvetica-boldoblique",
            "times-roman", "times-bold", "times-italic", "times-bolditalic",
            "symbol", "zapfdingbats",
        }
        reserved = {
            "china-t", "china-s", "japan", "japan-s", "japan-m",
            "korea", "korea-s", "korea-m",
        }
        return lowered in base14 or lowered in reserved or lowered.startswith(("ftnr", "funi", "fmath", "fsys", "fsel", "fdoc"))

    def _extract_pdf_font_content(self, xref: int) -> Optional[Tuple[str, bytes]]:
        w = self.window
        doc = getattr(w, "doc", None)
        base_path = getattr(w, "base_path", None)
        if doc is None or base_path is None or int(xref) <= 0:
            return None

        cache = getattr(w, "_pdf_font_content_cache", None)
        if cache is None:
            cache = {}
            w._pdf_font_content_cache = cache

        key = (str(base_path), int(xref))
        if key in cache:
            return cache[key]

        info = None
        try:
            info = doc.extract_font(int(xref))
        except Exception:
            cache[key] = None
            return None

        name = ""
        content = b""
        try:
            if isinstance(info, dict):
                name = str(info.get("name") or info.get("basefont") or "")
                raw = info.get("content") or info.get("buffer") or b""
                content = bytes(raw) if raw else b""
            elif isinstance(info, (tuple, list)):
                if len(info) >= 4:
                    name = str(info[0] or "")
                    raw = info[3]
                    content = bytes(raw) if raw else b""
            else:
                raw = getattr(info, "content", None) or getattr(info, "buffer", None)
                if raw:
                    name = str(getattr(info, "name", "") or "")
                    content = bytes(raw)
        except Exception:
            cache[key] = None
            return None

        if not content:
            cache[key] = None
            return None

        result = (name, content)
        cache[key] = result
        return result

    def _font_supports_text_buffer(self, font_buffer: bytes, text: str, source_font_name: str = "") -> bool:
        if not text:
            return True
        text = self._encode_display_text_for_font(text, source_font_name or "")
        try:
            font = fitz.Font(fontbuffer=font_buffer)
        except Exception:
            return False

        seen = set()
        for ch in text or "":
            if not ch.strip():
                continue
            seen.add(ch)
            if len(seen) >= 96:
                break

        for ch in seen:
            try:
                if not font.has_glyph(ord(ch)):
                    return False
            except Exception:
                return False
        return True

    def _embedded_pdf_font_plan(
        self,
        page_index: Optional[int],
        source_font_name: str,
        text: str,
        force_italic: bool = False,
        force_bold: bool = False,
    ) -> Optional[_ResolvedInsertFont]:
        w = self.window
        doc = getattr(w, "doc", None)
        if doc is None or page_index is None:
            return None
        if int(page_index) < 0 or int(page_index) >= len(doc):
            return None

        try:
            page_fonts = doc[int(page_index)].get_fonts(full=False)
        except Exception:
            return None

        desired_bold = bool(force_bold)
        desired_italic = bool(force_italic)
        source_bold = self._font_is_bold(source_font_name)
        source_italic = self._font_is_italic(source_font_name)
        style_override_requested = (desired_bold != source_bold) or (desired_italic != source_italic)

        best = None
        for entry in page_fonts or []:
            if len(entry) < 5:
                continue
            try:
                xref = int(entry[0] or 0)
            except Exception:
                xref = 0
            if xref <= 0:
                continue

            basefont_raw = str(entry[3] or "")
            resource_name = str(entry[4] or "")
            candidates = [
                self._strip_pdf_subset_prefix(basefont_raw),
                basefont_raw,
                resource_name,
            ]
            valid_candidates = [candidate for candidate in candidates if candidate]
            if not valid_candidates:
                continue
            candidate_bold = any(self._font_is_bold(candidate) for candidate in valid_candidates)
            candidate_italic = any(self._font_is_italic(candidate) for candidate in valid_candidates)
            name_score = min(self._font_name_pair_score(source_font_name, candidate) for candidate in valid_candidates)
            score = (
                0 if candidate_bold == desired_bold else 1,
                0 if candidate_italic == desired_italic else 1,
            ) + name_score
            if best is None or score < best[0]:
                best = (
                    score,
                    xref,
                    candidates[0] or resource_name or source_font_name,
                    candidate_bold,
                    candidate_italic,
                )

        if best is None or best[0][2] >= 3:
            return None
        if style_override_requested and (best[0][0] != 0 or best[0][1] != 0):
            return None

        extracted = self._extract_pdf_font_content(best[1])
        if not extracted:
            return None

        extracted_name, font_buffer = extracted
        if not self._font_supports_text_buffer(font_buffer, text, source_font_name=source_font_name):
            return None

        alias_name = self._font_alias_from_value(
            "fdoc",
            extracted_name or best[2] or source_font_name,
            force_italic=force_italic,
            force_bold=force_bold,
        )
        return _ResolvedInsertFont(
            font_name=alias_name,
            font_buffer=font_buffer,
            measure_font_buffer=font_buffer,
            source_font_name=source_font_name,
        )

    def _contains_hangul(self, text: str) -> bool:
        for ch in text or "":
            code = ord(ch)
            if (
                0x1100 <= code <= 0x11FF
                or 0x3130 <= code <= 0x318F
                or 0xA960 <= code <= 0xA97F
                or 0xAC00 <= code <= 0xD7AF
                or 0xD7B0 <= code <= 0xD7FF
            ):
                return True
        return False

    def _font_search_roots(self) -> List[Path]:
        roots = [
            Path.home() / "Library/Fonts",
            Path("/Library/Fonts"),
            Path("/System/Library/Fonts"),
            Path("/usr/local/share/fonts"),
            Path("/usr/share/fonts"),
        ]
        out = []
        seen = set()
        for root in roots:
            if not root.exists():
                continue
            key = str(root.resolve())
            if key in seen:
                continue
            seen.add(key)
            out.append(root)
        return out

    def _font_files(self) -> List[Path]:
        w = self.window
        cached = getattr(w, "_system_font_files_cache", None)
        if cached is not None:
            return cached

        out: List[Path] = []
        seen = set()
        exts = {".ttf", ".otf", ".ttc", ".otc"}
        for root in self._font_search_roots():
            for path in sorted(root.rglob("*")):
                if path.suffix.lower() not in exts:
                    continue
                try:
                    resolved = str(path.resolve())
                except Exception:
                    resolved = str(path)
                if resolved in seen:
                    continue
                seen.add(resolved)
                out.append(path)

        w._system_font_files_cache = out
        return out

    def _fitz_font_for_path(self, path: Path) -> Optional[fitz.Font]:
        w = self.window
        cache = getattr(w, "_fitz_font_cache", None)
        if cache is None:
            cache = {}
            w._fitz_font_cache = cache

        key = str(path)
        if key in cache:
            return cache[key]

        try:
            font = fitz.Font(fontfile=str(path))
        except Exception:
            cache[key] = None
            return None

        cache[key] = font
        return font

    def _font_has_glyph(self, path: Path, codepoint: int) -> bool:
        w = self.window
        cache = getattr(w, "_font_glyph_cache", None)
        if cache is None:
            cache = {}
            w._font_glyph_cache = cache

        key = (str(path), int(codepoint))
        if key in cache:
            return cache[key]

        font = self._fitz_font_for_path(path)
        if font is None:
            cache[key] = False
            return False

        try:
            supported = bool(font.has_glyph(int(codepoint)))
        except Exception:
            supported = False
        cache[key] = supported
        return supported

    def _font_supports_text(self, path: Path, text: str) -> bool:
        seen = set()
        for ch in text or "":
            if not ch.strip():
                continue
            seen.add(ch)
            if len(seen) >= 96:
                break

        if not seen:
            return True

        for ch in seen:
            if not self._font_has_glyph(path, ord(ch)):
                return False
        return True

    def _font_display_name_from_path(self, path: Path, raw_name: str) -> str:
        name = " ".join((raw_name or "").split()).strip()
        if not name or name.startswith(".LastResort"):
            name = ""
        if name and ("?" in name) and (name.count("?") >= 3 or (name.count("?") * 2) >= len(name)):
            name = ""
        if not name:
            name = path.stem.replace("_", " ").replace("-", " ").strip()
        return name or path.stem

    def _font_style_flags(self, name: str, path: Path) -> Tuple[bool, bool]:
        lowered = f"{name} {path.name}".lower()
        is_bold = "bold" in lowered or "semibold" in lowered or "heavy" in lowered
        is_italic = "italic" in lowered or "oblique" in lowered
        return is_bold, is_italic

    def _system_font_resources(self) -> List[_SystemFontResource]:
        w = self.window
        cached = getattr(w, "_system_font_resources_cache", None)
        if cached is not None:
            return cached

        out: List[_SystemFontResource] = []
        seen = set()

        for path in self._font_files():
            font = self._fitz_font_for_path(path)
            if font is None:
                continue

            display_name = self._font_display_name_from_path(path, getattr(font, "name", ""))
            if display_name.lower().startswith(".lastresort"):
                continue

            is_bold, is_italic = self._font_style_flags(display_name, path)
            key = (self._normalize_font_lookup_key(display_name), is_bold, is_italic)
            if key in seen:
                continue
            seen.add(key)

            supports_ascii = self._font_has_glyph(path, ord("A"))
            supports_hangul = self._font_has_glyph(path, ord("가"))
            supports_math = self._font_has_glyph(path, ord("∑")) or self._font_has_glyph(path, ord("α"))
            if not (supports_ascii or supports_hangul or supports_math):
                continue

            out.append(
                _SystemFontResource(
                    path=path,
                    display_name=display_name,
                    family_hint=self._font_family_hint(display_name),
                    supports_ascii=supports_ascii,
                    supports_hangul=supports_hangul,
                    supports_math=supports_math,
                    is_bold=is_bold,
                    is_italic=is_italic,
                )
            )

        w._system_font_resources_cache = out
        return out

    def _font_priority_score(
        self,
        font: _SystemFontResource,
        text: str,
        prefer_serif: bool,
        source_font_name: str = "",
        force_italic: bool = False,
        force_bold: bool = False,
    ) -> Tuple[int, int, int, int, int, int, int, int, int, int, int, str]:
        contains_hangul = self._contains_hangul(text)
        lowered = font.display_name.lower()
        source_match = self._font_name_match_score(source_font_name, font)

        if contains_hangul:
            preferred_serif = [
                "applemyungjo",
                "nanummyeongjo",
                "kopubbatang",
                "batang",
                "timesnewroman",
                "times",
                "georgia",
                "baskerville",
                "aptosserif",
            ]
            preferred_sans = [
                "applesdgothicneo",
                "applegothic",
                "pretendard",
                "nanumgothic",
                "nanumsquare",
                "malgun",
                "arialunicode",
                "arial",
                "helvetica",
                "aptos",
                "sfpro",
                "sfns",
            ]
        else:
            preferred_serif = [
                "newbaskerville",
                "baskerville",
                "timesnewroman",
                "times",
                "georgia",
                "garamond",
                "minion",
                "palatino",
                "bookman",
                "aptosserif",
                "applemyungjo",
                "nanummyeongjo",
                "kopubbatang",
                "batang",
            ]
            preferred_sans = [
                "helvetica",
                "arial",
                "sfpro",
                "sfns",
                "univers",
                "frutiger",
                "futura",
                "grotesk",
                "aptos",
                "applesdgothicneo",
                "applegothic",
                "pretendard",
                "nanumgothic",
                "nanumsquare",
                "malgun",
                "arialunicode",
            ]
        preferred = preferred_serif if prefer_serif else preferred_sans

        preferred_index = len(preferred) + 1
        for idx, token in enumerate(preferred):
            if token in self._normalize_font_lookup_key(lowered):
                preferred_index = idx
                break

        family_penalty = 0
        if prefer_serif and font.family_hint != "times":
            family_penalty = 1
        if (not prefer_serif) and font.family_hint == "times":
            family_penalty = 1

        hangul_penalty = 0
        if contains_hangul and not font.supports_hangul:
            hangul_penalty = 1

        style_penalty = 0
        if force_bold and not font.is_bold:
            style_penalty += 1
        if force_italic and not font.is_italic:
            style_penalty += 1

        user_penalty = 1 if "/Users/" in str(font.path) else 2
        ascii_penalty = 0 if font.supports_ascii else 1

        return (
            source_match[0],
            source_match[1],
            source_match[2],
            source_match[3],
            source_match[4],
            hangul_penalty,
            family_penalty,
            style_penalty,
            preferred_index,
            user_penalty,
            ascii_penalty,
            font.display_name.lower(),
        )

    def _compatible_system_font_paths(
        self,
        text: str,
        prefer_serif: bool,
        source_font_name: str = "",
        force_italic: bool = False,
        force_bold: bool = False,
        limit: int = 18,
    ) -> List[Path]:
        if not text:
            return []

        contains_hangul = self._contains_hangul(text)
        needs_math = self._contains_math_unicode(text)
        out: List[Path] = []
        resources = sorted(
            self._system_font_resources(),
            key=lambda item: self._font_priority_score(
                item,
                text,
                prefer_serif=prefer_serif,
                source_font_name=source_font_name,
                force_italic=force_italic,
                force_bold=force_bold,
            ),
        )

        for resource in resources:
            if contains_hangul and not resource.supports_hangul:
                continue
            if needs_math and not resource.supports_math and not resource.supports_hangul:
                continue
            if not self._font_supports_text(resource.path, text):
                continue
            out.append(resource.path)
            if len(out) >= max(1, int(limit)):
                break
        return out

    def _iter_insert_font_plans(
        self,
        text: str,
        font_name: str,
        prefer_times_new_roman: bool = False,
        force_italic: bool = False,
        force_bold: bool = False,
        preferred_font_path: Optional[Path] = None,
        page_index: Optional[int] = None,
    ) -> List[_ResolvedInsertFont]:
        segments = self._parse_script_segments(text)
        requires_unicode = any(self._contains_non_ascii(seg_text) for seg_text, _ in segments)
        requires_math_unicode = any(self._contains_math_unicode(seg_text) for seg_text, _ in segments)
        prefer_serif = prefer_times_new_roman or self._font_family_hint(font_name) == "times"
        system_font_paths = self._compatible_system_font_paths(
            text,
            prefer_serif=prefer_serif,
            source_font_name=font_name,
            force_italic=force_italic,
            force_bold=force_bold,
        )

        plans: List[_ResolvedInsertFont] = []
        seen = set()

        def add_plan(
            font_name_value: str,
            font_path: Optional[Path] = None,
            font_buffer: Optional[bytes] = None,
            measure_font_path: Optional[Path] = None,
            measure_font_buffer: Optional[bytes] = None,
            source_font_name: Optional[str] = None,
        ):
            path_key = str(font_path) if font_path else ""
            buffer_key = len(font_buffer) if font_buffer else 0
            measure_key = str(measure_font_path) if measure_font_path else ""
            measure_buffer_key = len(measure_font_buffer) if measure_font_buffer else 0
            key = (font_name_value.lower(), path_key, buffer_key, measure_key, measure_buffer_key)
            if key in seen:
                return
            seen.add(key)
            plans.append(
                _ResolvedInsertFont(
                    font_name=font_name_value,
                    font_path=font_path,
                    font_buffer=font_buffer,
                    measure_font_path=measure_font_path,
                    measure_font_buffer=measure_font_buffer,
                    source_font_name=source_font_name or font_name,
                )
            )

        if preferred_font_path and preferred_font_path.exists() and self._font_supports_text(preferred_font_path, text):
            add_plan(
                self._font_alias_from_path("fsel", preferred_font_path, force_italic=force_italic, force_bold=force_bold),
                font_path=preferred_font_path,
                measure_font_path=preferred_font_path,
                source_font_name=font_name,
            )

        embedded_plan = self._embedded_pdf_font_plan(
            page_index=page_index,
            source_font_name=font_name,
            text=text,
            force_italic=force_italic,
            force_bold=force_bold,
        )
        if embedded_plan is not None:
            add_plan(
                embedded_plan.font_name,
                font_buffer=embedded_plan.font_buffer,
                measure_font_buffer=embedded_plan.measure_font_buffer,
                source_font_name=embedded_plan.source_font_name or font_name,
            )

        if font_name and self._is_builtin_insert_font_name(font_name) and not requires_unicode and not requires_math_unicode:
            add_plan(font_name.strip(), source_font_name=font_name)

        if requires_math_unicode:
            for path in self._math_fallback_font_paths(force_italic=force_italic):
                if not self._font_supports_text(path, text):
                    continue
                add_plan(
                    self._font_alias_from_path("fmath", path, force_italic=force_italic, force_bold=force_bold),
                    font_path=path,
                    measure_font_path=path,
                    source_font_name=font_name,
                )

        for path in system_font_paths:
            add_plan(
                self._font_alias_from_path("fsys", path, force_italic=force_italic, force_bold=force_bold),
                font_path=path,
                measure_font_path=path,
                source_font_name=font_name,
            )

        if prefer_serif:
            for path in self._ordered_times_new_roman_paths(force_italic=force_italic, force_bold=force_bold):
                if not self._font_supports_text(path, text):
                    continue
                add_plan(
                    self._font_alias_from_path("ftnr", path, force_italic=force_italic, force_bold=force_bold),
                    font_path=path,
                    measure_font_path=path,
                    source_font_name=font_name,
                )

        if requires_unicode:
            for path in self._unicode_fallback_font_paths():
                if not self._font_supports_text(path, text):
                    continue
                add_plan(
                    self._font_alias_from_path("funi", path, force_italic=force_italic, force_bold=force_bold),
                    font_path=path,
                    measure_font_path=path,
                    source_font_name=font_name,
                )

        if not requires_unicode:
            for candidate in self._font_candidates(font_name, force_italic=force_italic, force_bold=force_bold):
                add_plan(candidate, source_font_name=font_name)

        return plans

    def _resolve_insert_font_plan(
        self,
        text: str,
        font_name: str,
        prefer_times_new_roman: bool = False,
        force_italic: bool = False,
        force_bold: bool = False,
        preferred_font_path: Optional[Path] = None,
        page_index: Optional[int] = None,
    ) -> Optional[_ResolvedInsertFont]:
        plans = self._iter_insert_font_plans(
            text,
            font_name,
            prefer_times_new_roman=prefer_times_new_roman,
            force_italic=force_italic,
            force_bold=force_bold,
            preferred_font_path=preferred_font_path,
            page_index=page_index,
        )
        if plans:
            return plans[0]
        return None

    def font_dialog_options(self, target: SimpleTextTarget) -> List[Tuple[str, str]]:
        options: List[Tuple[str, str]] = [("__auto__", "자동 (원본 스타일 + 문자셋 기준)")]
        for resource in sorted(
            self._system_font_resources(),
            key=lambda item: self._font_priority_score(
                item,
                target.text,
                prefer_serif=self._font_family_hint(target.font_name) == "times",
                source_font_name=target.font_name,
                force_italic=target.is_italic,
                force_bold=target.is_bold,
            ),
        ):
            if not resource.supports_ascii:
                continue
            suffix = []
            if resource.supports_hangul:
                suffix.append("한글")
            suffix.append("serif" if resource.family_hint == "times" else "sans")
            if resource.is_bold:
                suffix.append("bold")
            if resource.is_italic:
                suffix.append("italic")
            label = f"{resource.display_name} [{', '.join(suffix)}]"
            options.append((str(resource.path), label))
        return options

    def _locate_hit_span(self, page_idx: int, click_point: fitz.Point) -> Tuple[List[SpanInfo], Optional[SpanInfo], int]:
        w = self.window
        spans = w.current_spans_by_page.get(page_idx) or w._get_page_base_spans(page_idx)
        if not spans:
            return [], None, -1

        best_span = None
        best_score = None
        hit_char_index = -1

        for span in spans:
            if not fitz.Rect(span.rect).contains(click_point):
                continue

            local_hit_idx = -1
            for idx, ch in enumerate(span.chars):
                if (fitz.Rect(ch.rect) + (-0.3, -0.3, 0.3, 0.3)).contains(click_point):
                    local_hit_idx = idx
                    break

            score = self._span_hit_score(span, click_point, local_hit_idx)
            if best_span is None or score < best_score:
                best_span = span
                best_score = score
                hit_char_index = local_hit_idx

        if best_span is None:
            tol = 1.6
            for span in spans:
                if not (fitz.Rect(span.rect) + (-tol, -tol, tol, tol)).contains(click_point):
                    continue

                local_hit_idx = -1
                for idx, ch in enumerate(span.chars):
                    if (fitz.Rect(ch.rect) + (-tol, -tol, tol, tol)).contains(click_point):
                        local_hit_idx = idx
                        break

                score = self._span_hit_score(span, click_point, local_hit_idx)
                if best_span is None or score < best_score:
                    best_span = span
                    best_score = score
                    hit_char_index = local_hit_idx

        if best_span is None:
            nearest_span = None
            nearest_hit_idx = -1
            nearest_dist = None
            nearest_score = None
            tol = 4.0
            max_dist_sq = tol * tol

            for span in spans:
                span_rect = fitz.Rect(span.rect)
                span_dist = self._rect_distance_sq(span_rect, click_point)
                if span_dist > max_dist_sq:
                    continue

                local_hit_idx = -1
                local_dist = span_dist
                for idx, ch in enumerate(span.chars):
                    ch_rect = fitz.Rect(ch.rect)
                    ch_dist = self._rect_distance_sq(ch_rect, click_point)
                    if ch_dist <= max_dist_sq and (local_hit_idx < 0 or ch_dist < local_dist):
                        local_hit_idx = idx
                        local_dist = ch_dist

                score = self._span_hit_score(span, click_point, local_hit_idx)
                if (
                    nearest_span is None
                    or local_dist < nearest_dist
                    or (abs(local_dist - nearest_dist) < 1e-6 and score < nearest_score)
                ):
                    nearest_span = span
                    nearest_hit_idx = local_hit_idx
                    nearest_dist = local_dist
                    nearest_score = score

            if nearest_span is not None:
                best_span = nearest_span
                hit_char_index = nearest_hit_idx

        return spans, best_span, hit_char_index

    def _find_text_target_at_point(self, page_idx: int, click_point: fitz.Point, select_line: bool = False) -> Optional[SimpleTextTarget]:
        spans, best_span, hit_char_index = self._locate_hit_span(page_idx, click_point)

        if best_span is None:
            return None

        if select_line:
            line_spans = [s for s in spans if s.line_index == best_span.line_index]
            if line_spans:
                line_spans.sort(key=lambda s: (float(s.rect.x0), float(s.rect.x1)))
                line_rect = fitz.Rect(line_spans[0].rect)
                for s in line_spans[1:]:
                    line_rect |= fitz.Rect(s.rect)

                line_parts = []
                prev_rect = None
                for s in line_spans:
                    s_text = (s.text or "").strip()
                    if not s_text:
                        continue
                    if prev_rect is not None:
                        gap = float(s.rect.x0) - float(prev_rect.x1)
                        threshold = max(0.5, float(best_span.font_size) * 0.2)
                        if gap > threshold:
                            line_parts.append(" ")
                    line_parts.append(s_text)
                    prev_rect = s.rect

                line_text = "".join(line_parts).strip()
                if line_text:
                    first = line_spans[0]
                    return SimpleTextTarget(
                        page_index=first.page_index,
                        line_index=first.line_index,
                        rect=line_rect,
                        text=line_text,
                        origin=fitz.Point(float(first.rect.x0), float(first.origin.y)),
                        font_name=first.font_name or best_span.font_name or "Times-Roman",
                        font_size=float(first.font_size or best_span.font_size or 11.0),
                        color=int(first.color or best_span.color or 0),
                        is_italic=self._font_is_italic(first.font_name or ""),
                        is_bold=self._font_is_bold(first.font_name or ""),
                        span_index=int(first.span_index),
                        char_start=0,
                        char_end=-1,
                    )

        if 0 <= hit_char_index < len(best_span.chars):
            chars = best_span.chars
            token_idx = hit_char_index
            if not self._is_token_char(chars[token_idx].char):
                if token_idx > 0 and self._is_token_char(chars[token_idx - 1].char):
                    token_idx -= 1
                elif token_idx + 1 < len(chars) and self._is_token_char(chars[token_idx + 1].char):
                    token_idx += 1
                else:
                    if chars[token_idx].char.strip():
                        return self._make_char_target(best_span, chars[token_idx], token_idx)
                    return self._make_span_target(best_span)

            start = token_idx
            end = token_idx
            gap_split = max(0.75, float(best_span.font_size or 11.0) * 0.18)
            while start > 0 and self._is_token_char(chars[start - 1].char):
                if self._char_gap_x(chars[start - 1], chars[start]) > gap_split:
                    break
                start -= 1
            while end + 1 < len(chars) and self._is_token_char(chars[end + 1].char):
                if self._char_gap_x(chars[end], chars[end + 1]) > gap_split:
                    break
                end += 1

            token_chars = chars[start:end + 1]
            token_text = "".join(ch.char for ch in token_chars)
            if token_text.strip():
                token_rect = fitz.Rect(token_chars[0].rect)
                for ch in token_chars[1:]:
                    token_rect |= fitz.Rect(ch.rect)
                token_origin = fitz.Point(float(token_chars[0].rect.x0), float(best_span.origin.y))
                return SimpleTextTarget(
                    page_index=best_span.page_index,
                    line_index=best_span.line_index,
                    rect=token_rect,
                    text=token_text,
                    origin=token_origin,
                    font_name=best_span.font_name or "Times-Roman",
                    font_size=float(best_span.font_size or 11.0),
                    color=int(best_span.color or 0),
                    is_italic=self._font_is_italic(best_span.font_name or ""),
                    is_bold=self._font_is_bold(best_span.font_name or ""),
                    span_index=int(best_span.span_index),
                    char_start=int(start),
                    char_end=int(end),
                )

        return self._make_span_target(best_span)

    def find_text_target_at_point(
        self,
        page_idx: int,
        click_point: fitz.Point,
        select_line: bool = False,
    ) -> Optional[SimpleTextTarget]:
        return self._find_text_target_at_point(page_idx, click_point, select_line=select_line)

    def _font_candidates(self, font_name: str, force_italic: bool = False, force_bold: bool = False) -> List[str]:
        name = (font_name or "").strip()
        lowered = name.lower()
        base14 = {
            "courier", "courier-bold", "courier-oblique", "courier-boldoblique",
            "helvetica", "helvetica-bold", "helvetica-oblique", "helvetica-boldoblique",
            "times-roman", "times-bold", "times-italic", "times-bolditalic",
            "symbol", "zapfdingbats",
        }
        is_known_direct = lowered in base14 or lowered.startswith("ftnr") or lowered.startswith("funi")
        family_hint = self._font_family_hint(font_name)
        out = []

        def add_family_style(family: str):
            if family == "times":
                if force_bold and force_italic:
                    out.append("Times-BoldItalic")
                elif force_bold:
                    out.append("Times-Bold")
                elif force_italic:
                    out.append("Times-Italic")
                else:
                    out.append("Times-Roman")
            else:
                if force_bold and force_italic:
                    out.append("Helvetica-BoldOblique")
                elif force_bold:
                    out.append("Helvetica-Bold")
                elif force_italic:
                    out.append("Helvetica-Oblique")
                else:
                    out.append("Helvetica")

        add_family_style(family_hint)

        if name and is_known_direct:
            out.append(name)

        name_bold = "bold" in lowered
        name_italic = ("italic" in lowered) or ("oblique" in lowered)
        effective_bold = force_bold or name_bold
        effective_italic = force_italic or name_italic

        if family_hint == "times":
            if effective_bold and effective_italic:
                out.append("Times-BoldItalic")
            elif effective_bold:
                out.append("Times-Bold")
            elif effective_italic:
                out.append("Times-Italic")
            else:
                out.append("Times-Roman")
        else:
            if effective_bold and effective_italic:
                out.append("Helvetica-BoldOblique")
            elif effective_bold:
                out.append("Helvetica-Bold")
            elif effective_italic:
                out.append("Helvetica-Oblique")
            else:
                out.append("Helvetica")

        if family_hint == "times":
            if effective_bold and effective_italic:
                out.append("Helvetica-BoldOblique")
            elif effective_bold:
                out.append("Helvetica-Bold")
            elif effective_italic:
                out.append("Helvetica-Oblique")
            else:
                out.append("Helvetica")
        else:
            if effective_bold and effective_italic:
                out.append("Times-BoldItalic")
            elif effective_bold:
                out.append("Times-Bold")
            elif effective_italic:
                out.append("Times-Italic")
            else:
                out.append("Times-Roman")

        out.extend(["Times-Roman", "Helvetica"])

        dedup = []
        seen = set()
        for item in out:
            key = item.lower()
            if key in seen:
                continue
            seen.add(key)
            dedup.append(item)
        return dedup

    def _rgb_from_span_color(self, color: int) -> Tuple[float, float, float]:
        try:
            value = int(color)
        except Exception:
            return (0.0, 0.0, 0.0)
        r = ((value >> 16) & 0xFF) / 255.0
        g = ((value >> 8) & 0xFF) / 255.0
        b = (value & 0xFF) / 255.0
        return (r, g, b)

    def _estimate_text_width(
        self,
        text: str,
        font_name: str,
        font_size: float,
        force_italic: bool = False,
        force_bold: bool = False,
        preferred_font_path: Optional[Path] = None,
        resolved_plan: Optional[_ResolvedInsertFont] = None,
    ) -> Optional[float]:
        if not text:
            return 0.0
        measure_path = resolved_plan.measure_font_path if resolved_plan is not None else preferred_font_path
        measure_buffer = resolved_plan.measure_font_buffer if resolved_plan is not None else None
        measure_name = resolved_plan.font_name if resolved_plan is not None else font_name

        if measure_buffer:
            try:
                font = fitz.Font(fontbuffer=measure_buffer)
                return float(font.text_length(text, fontsize=float(font_size)))
            except Exception:
                pass
        if measure_path:
            font = self._fitz_font_for_path(measure_path)
            if font is not None and self._font_supports_text(measure_path, text):
                try:
                    return float(font.text_length(text, fontsize=float(font_size)))
                except Exception:
                    pass
        if resolved_plan is not None and resolved_plan.font_path is None:
            try:
                return float(fitz.get_text_length(text, fontname=measure_name, fontsize=float(font_size)))
            except Exception:
                return None

        for name in self._font_candidates(font_name, force_italic=force_italic, force_bold=force_bold):
            try:
                return float(fitz.get_text_length(text, fontname=name, fontsize=float(font_size)))
            except Exception:
                continue
        return None

    def _normalize_simple_text_input(self, text: str) -> str:
        s = (text or "").replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
        replacements = [
            ("\\leftrightarrow", "↔"),
            ("\\Leftrightarrow", "⇔"),
            ("\\rightarrow", "→"),
            ("\\Rightarrow", "⇒"),
            ("\\leftarrow", "←"),
            ("\\Leftarrow", "⇐"),
            ("\\mapsto", "↦"),
            ("\\to", "→"),
            ("\\infty", "∞"),
            ("\\alpha", "α"),
            ("\\beta", "β"),
            ("\\gamma", "γ"),
            ("\\delta", "δ"),
            ("\\epsilon", "ε"),
            ("\\varepsilon", "ε"),
            ("\\zeta", "ζ"),
            ("\\eta", "η"),
            ("\\theta", "θ"),
            ("\\vartheta", "ϑ"),
            ("\\iota", "ι"),
            ("\\kappa", "κ"),
            ("\\lambda", "λ"),
            ("\\mu", "μ"),
            ("\\nu", "ν"),
            ("\\xi", "ξ"),
            ("\\pi", "π"),
            ("\\varpi", "ϖ"),
            ("\\rho", "ρ"),
            ("\\varrho", "ϱ"),
            ("\\sigma", "σ"),
            ("\\varsigma", "ς"),
            ("\\tau", "τ"),
            ("\\upsilon", "υ"),
            ("\\phi", "φ"),
            ("\\varphi", "ϕ"),
            ("\\chi", "χ"),
            ("\\psi", "ψ"),
            ("\\omega", "ω"),
            ("\\Gamma", "Γ"),
            ("\\Delta", "Δ"),
            ("\\Theta", "Θ"),
            ("\\Lambda", "Λ"),
            ("\\Xi", "Ξ"),
            ("\\Pi", "Π"),
            ("\\Sigma", "Σ"),
            ("\\Upsilon", "Υ"),
            ("\\Phi", "Φ"),
            ("\\Psi", "Ψ"),
            ("\\Omega", "Ω"),
            ("\\pm", "±"),
            ("\\mp", "∓"),
            ("\\times", "×"),
            ("\\div", "÷"),
            ("\\cdot", "·"),
            ("\\ast", "∗"),
            ("\\circ", "∘"),
            ("\\leq", "≤"),
            ("\\geq", "≥"),
            ("\\neq", "≠"),
            ("\\approx", "≈"),
            ("\\equiv", "≡"),
            ("\\propto", "∝"),
            ("\\in", "∈"),
            ("\\notin", "∉"),
            ("\\subseteq", "⊆"),
            ("\\subset", "⊂"),
            ("\\supseteq", "⊇"),
            ("\\supset", "⊃"),
            ("\\cup", "∪"),
            ("\\cap", "∩"),
            ("\\forall", "∀"),
            ("\\exists", "∃"),
            ("\\nexists", "∄"),
            ("\\emptyset", "∅"),
            ("\\varnothing", "∅"),
            ("\\partial", "∂"),
            ("\\nabla", "∇"),
            ("\\sum", "∑"),
            ("\\prod", "∏"),
            ("\\int", "∫"),
            ("\\iint", "∬"),
            ("\\iiint", "∭"),
            ("\\sqrt", "√"),
            ("\\angle", "∠"),
            ("\\perp", "⟂"),
            ("\\parallel", "∥"),
            ("\\mid", "∣"),
            ("\\nmid", "∤"),
            ("\\sim", "∼"),
            ("\\simeq", "≃"),
            ("\\cong", "≅"),
            ("\\oplus", "⊕"),
            ("\\otimes", "⊗"),
            ("\\ominus", "⊖"),
            ("\\oslash", "⊘"),
            ("\\wedge", "∧"),
            ("\\vee", "∨"),
            ("\\neg", "¬"),
            ("\\land", "∧"),
            ("\\lor", "∨"),
            ("\\therefore", "∴"),
            ("\\because", "∵"),
            ("\\ldots", "…"),
            ("\\cdots", "⋯"),
            ("\\vdots", "⋮"),
            ("\\ddots", "⋱"),
            ("\\aleph", "ℵ"),
            ("\\Re", "ℜ"),
            ("\\Im", "ℑ"),
            ("\\wp", "℘"),
            ("\\hbar", "ℏ"),
            ("\\ell", "ℓ"),
            ("\\in", "∈"),
            ("\\ni", "∋"),
            ("\\notin", "∉"),
            ("\\owns", "∋"),
            ("\\subsetneq", "⊊"),
            ("\\supsetneq", "⊋"),
            ("\\setminus", "∖"),
            ("\\smallsetminus", "∖"),
            ("\\opencurlyeqprec", "≺"),
            ("\\opencurlyeqsucc", "≻"),
            ("\\degree", "°"),
            ("<->", "↔"),
            ("=>", "⇒"),
            ("->", "→"),
            ("<-", "←"),
            (">=", "≥"),
            ("<=", "≤"),
            ("!=", "≠"),
            ("+-", "±"),
        ]
        for a, b in replacements:
            s = s.replace(a, b)
        s = re.sub(r"\binfty\b", "∞", s, flags=re.IGNORECASE)
        return s

    def _contains_math_unicode(self, text: str) -> bool:
        for ch in text or "":
            if ord(ch) <= 126:
                continue
            category = unicodedata.category(ch)
            name = unicodedata.name(ch, "")
            if category == "Sm":
                return True
            if "GREEK" in name or "MATHEMATICAL" in name:
                return True
            if ch in "∞∈∉∋⊂⊃⊆⊇∪∩∀∃∄∅∂∇∑∏∫∬∭√≤≥≠≈≡∝∧∨¬∥⟂∠⊕⊗⊖⊘ℵℜℑ℘ℏ⋯⋮⋱":
                return True
        return False

    def _math_fallback_font_paths(self, force_italic: bool = False) -> List[Path]:
        candidates = [
            "/System/Library/Fonts/Supplemental/STIXTwoMath.otf",
            "/System/Library/Fonts/Supplemental/STIXGeneral.otf",
            "/System/Library/Fonts/Supplemental/STIXGeneralItalic.otf" if force_italic else "",
            "/System/Library/Fonts/Apple Symbols.ttf",
            "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
            "/System/Library/Fonts/Symbol.ttf",
        ]
        out: List[Path] = []
        seen = set()
        for p in candidates:
            if not p:
                continue
            path = Path(p)
            if not path.exists():
                continue
            key = str(path.resolve())
            if key in seen:
                continue
            seen.add(key)
            out.append(path)
        return out

    def _unicode_fallback_font_paths(self) -> List[Path]:
        w = self.window
        if w._unicode_font_paths_cache is not None:
            return w._unicode_font_paths_cache

        candidates = [
            "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
            "/System/Library/Fonts/Apple Symbols.ttf",
            "/System/Library/Fonts/Supplemental/STIXTwoMath-Regular.otf",
            "/Library/Fonts/Arial Unicode.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
        out: List[Path] = []
        seen = set()
        for p in candidates:
            path = Path(p)
            if not path.exists():
                continue
            key = str(path.resolve())
            if key in seen:
                continue
            seen.add(key)
            out.append(path)
        w._unicode_font_paths_cache = out
        return out

    def _times_new_roman_font_paths(self) -> List[Path]:
        w = self.window
        if w._times_new_roman_paths_cache is not None:
            return w._times_new_roman_paths_cache

        candidates = [
            "/System/Library/Fonts/Supplemental/Times New Roman.ttf",
            "/Library/Fonts/Times New Roman.ttf",
            "/Library/Fonts/Microsoft/Times New Roman.ttf",
            "/System/Library/Fonts/Supplemental/Times New Roman Bold.ttf",
            "/System/Library/Fonts/Supplemental/Times New Roman Italic.ttf",
            "/System/Library/Fonts/Supplemental/Times New Roman Bold Italic.ttf",
        ]
        out: List[Path] = []
        seen = set()
        for p in candidates:
            path = Path(p)
            if not path.exists():
                continue
            key = str(path.resolve())
            if key in seen:
                continue
            seen.add(key)
            out.append(path)
        w._times_new_roman_paths_cache = out
        return out

    def _ordered_times_new_roman_paths(self, force_italic: bool = False, force_bold: bool = False) -> List[Path]:
        paths = list(self._times_new_roman_font_paths())
        if not force_italic and not force_bold:
            return paths

        def is_italic(p: Path) -> bool:
            return "italic" in p.name.lower()

        def is_bold(p: Path) -> bool:
            return "bold" in p.name.lower()

        if force_bold and force_italic:
            both = [p for p in paths if is_bold(p) and is_italic(p)]
            bold_only = [p for p in paths if is_bold(p) and not is_italic(p)]
            italic_only = [p for p in paths if is_italic(p) and not is_bold(p)]
            normal = [p for p in paths if not is_bold(p) and not is_italic(p)]
            return both + bold_only + italic_only + normal
        if force_bold:
            bold_only = [p for p in paths if is_bold(p) and not is_italic(p)]
            both = [p for p in paths if is_bold(p) and is_italic(p)]
            normal = [p for p in paths if not is_bold(p)]
            return bold_only + both + normal
        if force_italic:
            italic_only = [p for p in paths if is_italic(p) and not is_bold(p)]
            both = [p for p in paths if is_italic(p) and is_bold(p)]
            normal = [p for p in paths if not is_italic(p)]
            return italic_only + both + normal
        return paths

    def _contains_non_ascii(self, text: str) -> bool:
        return any(ord(ch) > 126 for ch in (text or ""))

    def _parse_script_segments(self, text: str) -> List[Tuple[str, str]]:
        src = text or ""
        raw: List[Tuple[str, str]] = []
        i = 0
        while i < len(src):
            ch = src[i]
            if ch == "\\" and i + 1 < len(src) and src[i + 1] in "\\^_{}":
                raw.append((src[i + 1], "normal"))
                i += 2
                continue

            if ch in ("^", "_"):
                script = "super" if ch == "^" else "sub"
                if i + 1 >= len(src):
                    raw.append((ch, "normal"))
                    i += 1
                    continue

                if src[i + 1] == "{":
                    depth = 1
                    j = i + 2
                    while j < len(src):
                        if src[j] == "\\" and j + 1 < len(src):
                            j += 2
                            continue
                        if src[j] == "{":
                            depth += 1
                        elif src[j] == "}":
                            depth -= 1
                            if depth == 0:
                                break
                        j += 1
                    if j < len(src) and depth == 0:
                        payload = src[i + 2:j]
                        if payload:
                            raw.append((payload, script))
                        i = j + 1
                        continue

                raw.append((src[i + 1], script))
                i += 2
                continue

            raw.append((ch, "normal"))
            i += 1

        merged: List[Tuple[str, str]] = []
        for seg_text, seg_mode in raw:
            if not seg_text:
                continue
            if merged and merged[-1][1] == seg_mode:
                merged[-1] = (merged[-1][0] + seg_text, seg_mode)
            else:
                merged.append((seg_text, seg_mode))
        return merged

    def _script_font_size(self, base_size: float, script_mode: str) -> float:
        if script_mode == "normal":
            return float(base_size)
        if script_mode == "super":
            return max(4.0, float(base_size) * 0.58)
        return max(4.0, float(base_size) * 0.58)

    def _script_shifts(self, base_size: float, origin_y: float, target_rect: Optional[fitz.Rect]) -> Tuple[float, float]:
        default_up = float(base_size) * 0.40
        default_down = float(base_size) * 0.22
        return default_up, default_down

    def _page_line_metrics(self, page_idx: int) -> List[Tuple[int, float, float, float]]:
        w = self.window
        spans = w.current_spans_by_page.get(page_idx) or w._get_page_base_spans(page_idx)
        if not spans:
            return []

        grouped: Dict[int, Dict[str, float]] = {}
        for span in spans:
            entry = grouped.setdefault(
                int(span.line_index),
                {
                    "top": float(span.rect.y0),
                    "bottom": float(span.rect.y1),
                    "baseline_sum": 0.0,
                    "count": 0.0,
                },
            )
            baseline_y = float(span.origin.y)
            metric_top, metric_bottom = self._compact_vertical_bounds(
                baseline_y,
                float(span.font_size or 11.0),
                float(span.ascender or 0.0),
                float(span.descender or 0.0),
            )
            entry["top"] = min(entry["top"], metric_top)
            entry["bottom"] = max(entry["bottom"], metric_bottom)
            entry["baseline_sum"] += float(span.origin.y)
            entry["count"] += 1.0

        out: List[Tuple[int, float, float, float]] = []
        for line_index, entry in grouped.items():
            baseline = entry["baseline_sum"] / max(1.0, entry["count"])
            out.append((line_index, baseline, entry["top"], entry["bottom"]))
        out.sort(key=lambda item: item[1])
        return out

    def _line_vertical_bounds(self, target: SimpleTextTarget) -> Tuple[float, float]:
        rect = fitz.Rect(target.rect)
        baseline = float(target.origin.y)
        size = max(2.0, float(target.font_size or 11.0))

        top, bottom = self._compact_vertical_bounds(baseline, size)

        lines = self._page_line_metrics(target.page_index)
        current_idx = -1
        for i, (line_index, line_baseline, line_top, line_bottom) in enumerate(lines):
            if line_index == target.line_index:
                current_idx = i
                top = line_top
                bottom = line_bottom
                baseline = line_baseline
                break

        if current_idx >= 0:
            if current_idx > 0:
                prev_baseline = float(lines[current_idx - 1][1])
                top = max(top, (prev_baseline + baseline) * 0.5)
            if current_idx + 1 < len(lines):
                next_baseline = float(lines[current_idx + 1][1])
                bottom = min(bottom, (baseline + next_baseline) * 0.5)

        pad = max(0.3, size * 0.03)
        top = min(top + pad, baseline - max(1.0, size * 0.08))
        bottom = max(bottom - pad, baseline + max(1.0, size * 0.08))

        if bottom - top < max(2.0, size * 0.42):
            top, bottom = self._compact_vertical_bounds(baseline, size)

        return top, bottom

    def _effective_edit_rect(self, target: SimpleTextTarget, allow_overflow_right: bool) -> fitz.Rect:
        rect = fitz.Rect(target.rect)
        size = max(2.0, float(target.font_size or 11.0))
        baseline = float(target.origin.y)
        asc = float(getattr(target, "ascender", 0.0) or 0.0)
        desc = float(getattr(target, "descender", 0.0) or 0.0)
        top_ratio = 0.84
        bottom_ratio = 0.30
        if asc > 0:
            top_ratio = min(0.92, max(0.80, asc * 0.95))
        if desc < 0:
            bottom_ratio = min(0.38, max(0.24, (-desc) * 0.70))
        rect.y0 = baseline - size * top_ratio
        rect.y1 = baseline + size * bottom_ratio
        return rect

    def _is_safe_edit_rect(self, target: SimpleTextTarget, edit_rect: fitz.Rect) -> bool:
        return not fitz.Rect(edit_rect).is_empty

    def _has_neighbor_overlap_conflict(self, target: SimpleTextTarget, edit_rect: fitz.Rect) -> bool:
        return False

    def _estimate_scripted_text_width(
        self,
        text: str,
        font_name: str,
        base_size: float,
        force_italic: bool = False,
        force_bold: bool = False,
        preferred_font_path: Optional[Path] = None,
        resolved_plan: Optional[_ResolvedInsertFont] = None,
    ) -> Optional[float]:
        text = self._text_for_resolved_plan(text, resolved_plan)
        segments = self._parse_script_segments(text)
        if not segments:
            return 0.0

        total = 0.0
        for seg_text, seg_mode in segments:
            seg_size = self._script_font_size(base_size, seg_mode)
            seg_width = self._estimate_text_width(
                seg_text,
                font_name,
                seg_size,
                force_italic=force_italic,
                force_bold=force_bold,
                preferred_font_path=preferred_font_path,
                resolved_plan=resolved_plan,
            )
            if seg_width is None:
                return None
            total += seg_width
        return total

    def _measure_insert_segment_width(
        self,
        text: str,
        font_name: str,
        font_size: float,
        measure_font_path: Optional[Path] = None,
        measure_font_buffer: Optional[bytes] = None,
    ) -> float:
        if measure_font_buffer:
            try:
                font = fitz.Font(fontbuffer=measure_font_buffer)
                return float(font.text_length(text, fontsize=float(font_size)))
            except Exception:
                pass
        if measure_font_path:
            font = self._fitz_font_for_path(measure_font_path)
            if font is not None:
                try:
                    return float(font.text_length(text, fontsize=float(font_size)))
                except Exception:
                    pass
        try:
            return float(fitz.get_text_length(text, fontname=font_name, fontsize=float(font_size)))
        except Exception:
            fallback = self._estimate_text_width(
                text,
                font_name,
                font_size,
                preferred_font_path=measure_font_path,
            )
            if fallback is not None:
                return float(fallback)
            return max(0.0, float(font_size) * 0.52 * len(text))

    def _insert_script_segments(
        self,
        page: fitz.Page,
        origin: fitz.Point,
        text: str,
        font_name: str,
        base_size: float,
        color: Tuple[float, float, float],
        insert_kwargs: Dict,
        target_rect: Optional[fitz.Rect],
        super_shift_em: Optional[float] = None,
        sub_shift_em: Optional[float] = None,
        measure_font_path: Optional[Path] = None,
        measure_font_buffer: Optional[bytes] = None,
    ) -> bool:
        segments = self._parse_script_segments(text)
        if not segments:
            return True

        x = float(origin.x)
        y0 = float(origin.y)
        up_shift, down_shift = self._script_shifts(base_size, y0, target_rect)
        if super_shift_em is not None:
            up_shift = float(base_size) * float(super_shift_em)
        if sub_shift_em is not None:
            down_shift = float(base_size) * float(sub_shift_em)

        for seg_text, seg_mode in segments:
            seg_size = self._script_font_size(base_size, seg_mode)
            y = y0
            if seg_mode == "super":
                y -= up_shift
            elif seg_mode == "sub":
                y += down_shift

            page.insert_text(
                fitz.Point(x, y),
                seg_text,
                fontname=font_name,
                fontsize=float(seg_size),
                color=color,
                **insert_kwargs,
            )
            x += self._measure_insert_segment_width(
                seg_text,
                font_name,
                seg_size,
                measure_font_path=measure_font_path,
                measure_font_buffer=measure_font_buffer,
            )
        return True

    def _insert_with_plan(
        self,
        page: fitz.Page,
        origin: fitz.Point,
        text: str,
        base_size: float,
        color: Tuple[float, float, float],
        insert_kwargs: Dict,
        target_rect: Optional[fitz.Rect],
        resolved_plan: _ResolvedInsertFont,
        super_shift_em: Optional[float] = None,
        sub_shift_em: Optional[float] = None,
    ) -> bool:
        if resolved_plan.font_path is not None:
            page.insert_font(fontname=resolved_plan.font_name, fontfile=str(resolved_plan.font_path))
        elif resolved_plan.font_buffer is not None:
            page.insert_font(fontname=resolved_plan.font_name, fontbuffer=resolved_plan.font_buffer)

        text = self._text_for_resolved_plan(text, resolved_plan)
        self._insert_script_segments(
            page=page,
            origin=origin,
            text=text,
            font_name=resolved_plan.font_name,
            base_size=base_size,
            color=color,
            insert_kwargs=insert_kwargs,
            target_rect=target_rect,
            super_shift_em=super_shift_em,
            sub_shift_em=sub_shift_em,
            measure_font_path=resolved_plan.measure_font_path,
            measure_font_buffer=resolved_plan.measure_font_buffer,
        )
        return True

    def _draw_vector_arrow_in_rect(
        self,
        page: fitz.Page,
        target_rect: fitz.Rect,
        color: Tuple[float, float, float],
        base_size: float,
        text_width: Optional[float] = None,
        baseline_y: Optional[float] = None,
        vector_shift_em: Optional[float] = None,
    ):
        rect = fitz.Rect(target_rect)
        if rect.is_empty or rect.width <= 1.0 or rect.height <= 1.0:
            return

        pad_x = max(0.35, float(base_size) * 0.04)
        if baseline_y is None:
            baseline_y = float(rect.y1) - float(base_size) * 0.22
        shift_em = 0.80 if vector_shift_em is None else float(vector_shift_em)
        y = float(baseline_y) - float(base_size) * shift_em
        x0 = float(rect.x0) + pad_x
        usable = max(0.0, float(rect.width) - pad_x * 2.0)
        if usable <= 1.2:
            return

        desired = float(text_width) if text_width and text_width > 0 else usable * 0.72
        length = min(usable, max(float(base_size) * 1.02, desired))
        x1 = x0 + length
        if x1 <= x0 + 0.8:
            return

        line_w = max(0.45, float(base_size) * 0.03)
        page.draw_line(fitz.Point(x0, y), fitz.Point(x1, y), color=color, width=line_w, overlay=True)

        head = min(max(2.0, float(base_size) * 0.28), max(1.4, length * 0.30))
        hx = x1 - head
        hy = head * 0.55
        up = y - hy
        down = y + hy
        page.draw_line(fitz.Point(hx, up), fitz.Point(x1, y), color=color, width=line_w, overlay=True)
        page.draw_line(fitz.Point(hx, down), fitz.Point(x1, y), color=color, width=line_w, overlay=True)

    def _line_spans_for_target(self, target: SimpleTextTarget) -> List[SpanInfo]:
        w = self.window
        spans = w.current_spans_by_page.get(target.page_index) or w._get_page_base_spans(target.page_index)
        line_spans = [span for span in spans if span.line_index == target.line_index]
        line_spans.sort(key=lambda span: (float(span.rect.x0), int(span.span_index), float(span.rect.x1)))
        return line_spans

    def _union_char_rects(self, chars: List[CharInfo]) -> fitz.Rect:
        if not chars:
            return fitz.Rect()
        rect = fitz.Rect(chars[0].rect)
        for ch in chars[1:]:
            rect |= fitz.Rect(ch.rect)
        return rect

    def _piece_erase_rect(self, rect: fitz.Rect, baseline_y: float, font_size: float) -> fitz.Rect:
        size = max(2.0, float(font_size or 11.0))
        out = fitz.Rect(rect)
        out.y0 = baseline_y - size * 0.84
        out.y1 = baseline_y + size * 0.30
        out.x0 -= max(0.5, size * 0.03)
        out.x1 += max(0.5, size * 0.03)
        return out

    def _sample_background_fill(
        self,
        page: fitz.Page,
        rect: fitz.Rect,
        ignore_rgb: Optional[Tuple[float, float, float]] = None,
    ) -> Tuple[float, float, float]:
        sample_rect = fitz.Rect(rect)
        if sample_rect.is_empty:
            return (1.0, 1.0, 1.0)

        sample_rect &= page.rect
        if sample_rect.is_empty or sample_rect.width <= 1.0 or sample_rect.height <= 1.0:
            return (1.0, 1.0, 1.0)

        try:
            pix = page.get_pixmap(matrix=fitz.Matrix(3.0, 3.0), clip=sample_rect, alpha=False)
        except Exception:
            return (1.0, 1.0, 1.0)

        width = int(pix.width)
        height = int(pix.height)
        channels = int(pix.n)
        if width <= 0 or height <= 0 or channels < 3:
            return (1.0, 1.0, 1.0)

        counts: Dict[Tuple[int, int, int], int] = defaultdict(int)
        sums: Dict[Tuple[int, int, int], List[int]] = defaultdict(lambda: [0, 0, 0])
        samples = pix.samples
        ignore = None
        if ignore_rgb is not None:
            ignore = tuple(max(0, min(255, int(round(value * 255.0)))) for value in ignore_rgb)

        def add_pixel(px: int, py: int, skip_ignore: bool):
            if px < 0 or py < 0 or px >= width or py >= height:
                return
            idx = (py * width + px) * channels
            r = samples[idx]
            g = samples[idx + 1]
            b = samples[idx + 2]
            if skip_ignore and ignore is not None:
                dist = abs(r - ignore[0]) + abs(g - ignore[1]) + abs(b - ignore[2])
                if dist <= 72:
                    return
            key = (r // 8, g // 8, b // 8)
            counts[key] += 1
            sums[key][0] += r
            sums[key][1] += g
            sums[key][2] += b

        for py in range(height):
            for px in range(width):
                add_pixel(px, py, skip_ignore=True)

        if not counts:
            for py in range(height):
                for px in range(width):
                    add_pixel(px, py, skip_ignore=False)
            if not counts:
                return (1.0, 1.0, 1.0)

        best_key = max(
            counts.keys(),
            key=lambda key: (
                counts[key],
                (key[0] + key[1] + key[2]),
            ),
        )
        total = max(1, counts[best_key])
        avg = sums[best_key]
        return (
            avg[0] / total / 255.0,
            avg[1] / total / 255.0,
            avg[2] / total / 255.0,
        )

    def _erase_text_rects(self, page: fitz.Page, regions: List[Tuple[fitz.Rect, Optional[Tuple[float, float, float]]]]):
        valid_regions = [(fitz.Rect(rect), rgb) for rect, rgb in regions if rect and not fitz.Rect(rect).is_empty]
        if not valid_regions:
            return

        for rect, ignore_rgb in valid_regions:
            fill = self._sample_background_fill(page, rect, ignore_rgb=ignore_rgb)
            page.add_redact_annot(rect, fill=fill, cross_out=False)

        page.apply_redactions(
            images=getattr(fitz, "PDF_REDACT_IMAGE_NONE", 0),
            graphics=getattr(fitz, "PDF_REDACT_LINE_ART_NONE", 0),
            text=getattr(fitz, "PDF_REDACT_TEXT_REMOVE", 0),
        )

    def _build_reflow_suffix_pieces(self, target: SimpleTextTarget) -> List[_LineRerenderPiece]:
        if target.span_index < 0:
            return []

        pieces: List[_LineRerenderPiece] = []
        line_spans = self._line_spans_for_target(target)
        target_found = False

        for span in line_spans:
            if span.span_index < target.span_index:
                continue

            if span.span_index == target.span_index:
                target_found = True
                if target.char_end >= 0 and target.char_end + 1 < len(span.chars):
                    suffix_chars = span.chars[target.char_end + 1:]
                    pieces.extend(
                        self._chars_to_rerender_pieces(
                            chars=suffix_chars,
                            baseline_y=float(span.origin.y),
                            font_name=span.font_name or "Times-Roman",
                            font_size=float(span.font_size or 11.0),
                            color=int(span.color or 0),
                            is_italic=self._font_is_italic(span.font_name or ""),
                            is_bold=self._font_is_bold(span.font_name or ""),
                        )
                    )
                continue

            if target_found or float(span.rect.x0) >= float(target.rect.x1) - 0.2:
                pieces.extend(
                    self._chars_to_rerender_pieces(
                        chars=span.chars,
                        baseline_y=float(span.origin.y),
                        font_name=span.font_name or "Times-Roman",
                        font_size=float(span.font_size or 11.0),
                        color=int(span.color or 0),
                        is_italic=self._font_is_italic(span.font_name or ""),
                        is_bold=self._font_is_bold(span.font_name or ""),
                    )
                )

        return pieces

    def _layout_reflow_suffix_pieces(
        self,
        target: SimpleTextTarget,
        replacement_width: float,
        suffix_pieces: List[_LineRerenderPiece],
    ) -> List[Tuple[_LineRerenderPiece, fitz.Point]]:
        positioned: List[Tuple[_LineRerenderPiece, fitz.Point]] = []
        prev_original_end = float(target.rect.x1)
        prev_inserted_end = float(target.origin.x) + max(0.0, float(replacement_width))

        for piece in suffix_pieces:
            original_gap = max(0.0, float(piece.rect.x0) - prev_original_end)
            piece_plan = self._resolve_insert_font_plan(
                piece.text,
                piece.font_name,
                prefer_times_new_roman=self._font_family_hint(piece.font_name) == "times",
                force_italic=piece.is_italic,
                force_bold=piece.is_bold,
                page_index=target.page_index,
            )
            piece_width = self._estimate_scripted_text_width(
                piece.text,
                piece.font_name,
                piece.font_size,
                force_italic=piece.is_italic,
                force_bold=piece.is_bold,
                resolved_plan=piece_plan,
            )
            if piece_width is None:
                piece_width = max(0.0, float(piece.font_size) * 0.52 * len(piece.text))

            min_x = prev_inserted_end + original_gap
            shifted_x = float(piece.origin.x) + (float(replacement_width) - max(0.0, float(target.rect.width)))
            new_x = max(min_x, shifted_x)
            new_origin = fitz.Point(new_x, float(piece.origin.y))
            positioned.append((piece, new_origin))

            prev_original_end = float(piece.rect.x1)
            prev_inserted_end = new_x + float(piece_width)

        return positioned

    def _apply_simple_text_reflow_replace(
        self,
        target: SimpleTextTarget,
        new_text: str,
        force_italic: bool = False,
        force_bold: bool = False,
        draw_vector_arrow: bool = False,
        super_shift_em: Optional[float] = None,
        sub_shift_em: Optional[float] = None,
        vector_shift_em: Optional[float] = None,
        preferred_font_path: Optional[Path] = None,
    ) -> bool:
        w = self.window
        if not w._has_open_doc():
            return False

        replacement = self._normalize_simple_text_input(new_text)
        target_plan = self._resolve_insert_font_plan(
            replacement,
            target.font_name,
            prefer_times_new_roman=False,
            force_italic=force_italic,
            force_bold=force_bold,
            preferred_font_path=preferred_font_path,
            page_index=target.page_index,
        )
        width = self._estimate_scripted_text_width(
            replacement,
            target.font_name,
            target.font_size,
            force_italic=force_italic,
            force_bold=force_bold,
            preferred_font_path=preferred_font_path,
            resolved_plan=target_plan,
        )
        if width is None:
            width = max(0.0, float(target.font_size) * 0.52 * len(replacement))

        suffix_pieces = self._build_reflow_suffix_pieces(target)
        laid_out_suffix = self._layout_reflow_suffix_pieces(target, float(width), suffix_pieces)

        w._push_undo_snapshot()

        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
        tmp_path = Path(tmp.name)
        tmp.close()
        w._register_temp_file(tmp_path)

        src_doc = None
        try:
            src_doc = fitz.open(str(w.base_path))
            page = src_doc[target.page_index]

            target_erase_rect = self._piece_erase_rect(target.rect, float(target.origin.y), float(target.font_size))
            erase_regions = [(target_erase_rect, self._rgb_from_span_color(target.color))]
            for piece in suffix_pieces:
                erase_regions.append(
                    (
                        self._piece_erase_rect(piece.rect, float(piece.origin.y), float(piece.font_size)),
                        self._rgb_from_span_color(piece.color),
                    )
                )
            self._erase_text_rects(page, erase_regions)

            if replacement:
                rgb = self._rgb_from_span_color(target.color)
                inserted = self._insert_simple_text_with_fallback(
                    page=page,
                    origin=target.origin,
                    text=replacement,
                    font_name=target.font_name,
                    font_size=target.font_size,
                    color=rgb,
                    prefer_times_new_roman=False,
                    target_rect=target_erase_rect,
                    force_italic=force_italic,
                    force_bold=force_bold,
                    super_shift_em=super_shift_em,
                    sub_shift_em=sub_shift_em,
                    preferred_font_path=preferred_font_path,
                )
                if not inserted:
                    raise RuntimeError("텍스트 삽입 실패")
                if draw_vector_arrow:
                    arrow_rect = fitz.Rect(target_erase_rect)
                    arrow_rect.x1 = max(float(arrow_rect.x1), float(target.origin.x) + float(width))
                    self._draw_vector_arrow_in_rect(
                        page=page,
                        target_rect=arrow_rect,
                        color=rgb,
                        base_size=target.font_size,
                        text_width=width,
                        baseline_y=float(target.origin.y),
                        vector_shift_em=vector_shift_em,
                    )

            for piece, new_origin in laid_out_suffix:
                rgb = self._rgb_from_span_color(piece.color)
                piece_plan = self._resolve_insert_font_plan(
                    piece.text,
                    piece.font_name,
                    prefer_times_new_roman=self._font_family_hint(piece.font_name) == "times",
                    force_italic=piece.is_italic,
                    force_bold=piece.is_bold,
                    page_index=target.page_index,
                )
                new_rect = fitz.Rect(
                    float(new_origin.x),
                    float(piece.rect.y0),
                    float(new_origin.x) + max(float(piece.rect.width), 1.0),
                    float(piece.rect.y1),
                )
                inserted = self._insert_simple_text_with_fallback(
                    page=page,
                    origin=new_origin,
                    text=piece.text,
                    font_name=piece.font_name,
                    font_size=piece.font_size,
                    color=rgb,
                    prefer_times_new_roman=self._font_family_hint(piece.font_name) == "times",
                    target_rect=self._piece_erase_rect(
                        new_rect,
                        float(piece.origin.y),
                        float(piece.font_size),
                    ),
                    force_italic=piece.is_italic,
                    force_bold=piece.is_bold,
                )
                if not inserted:
                    raise RuntimeError("뒤쪽 텍스트 재배치 실패")

            src_doc.save(str(tmp_path))
            src_doc.close()
            src_doc = None

            saved_h = w.view.horizontalScrollBar().value()
            saved_v = w.view.verticalScrollBar().value()

            old_base = w.base_path
            old_temp = w.temp_margin_file
            w._activate_temp_pdf(tmp_path, old_base, old_temp)

            w.current_page_index = min(target.page_index, len(w.doc) - 1)
            w._thumbnail_selected_pages = {w.current_page_index}
            w._clear_search_state()
            w._mark_modified()
            w._refresh_thumbnail_sidebar(force=True)
            w._scroll_to_current_after_render = False
            w.render_page()
            w.view.horizontalScrollBar().setValue(saved_h)
            w.view.verticalScrollBar().setValue(saved_v)
            w.statusBar().showMessage("텍스트를 적용했습니다.", 1800)
            return True

        except Exception as e:
            if src_doc is not None:
                try:
                    src_doc.close()
                except Exception:
                    pass
            w._cleanup_temp_file(tmp_path)
            if w.undo_stack:
                w.undo_stack.pop()
            QMessageBox.critical(w, "텍스트 수정 실패", f"오류:\n{e}")
            return False

    def _insert_simple_text_with_fallback(
        self,
        page: fitz.Page,
        origin: fitz.Point,
        text: str,
        font_name: str,
        font_size: float,
        color: Tuple[float, float, float],
        prefer_times_new_roman: bool = False,
        target_rect: Optional[fitz.Rect] = None,
        force_italic: bool = False,
        force_bold: bool = False,
        super_shift_em: Optional[float] = None,
        sub_shift_em: Optional[float] = None,
        preferred_font_path: Optional[Path] = None,
    ) -> bool:
        insert_kwargs = {"overlay": True}
        utf8_enc = getattr(fitz, "TEXT_ENCODING_UTF8", None)
        if utf8_enc is not None:
            insert_kwargs["encoding"] = utf8_enc

        for plan in self._iter_insert_font_plans(
            text,
            font_name,
            prefer_times_new_roman=prefer_times_new_roman,
            force_italic=force_italic,
            force_bold=force_bold,
            preferred_font_path=preferred_font_path,
            page_index=getattr(page, "number", None),
        ):
            try:
                self._insert_with_plan(
                    page=page,
                    origin=origin,
                    text=text,
                    base_size=font_size,
                    color=color,
                    insert_kwargs=insert_kwargs,
                    target_rect=target_rect,
                    resolved_plan=plan,
                    super_shift_em=super_shift_em,
                    sub_shift_em=sub_shift_em,
                )
                return True
            except Exception:
                continue
        return False

    def _apply_simple_text_replace(
        self,
        target: SimpleTextTarget,
        new_text: str,
        allow_overflow_right: bool = False,
        force_italic: bool = False,
        force_bold: bool = False,
        draw_vector_arrow: bool = False,
        super_shift_em: Optional[float] = None,
        sub_shift_em: Optional[float] = None,
        vector_shift_em: Optional[float] = None,
        preferred_font_path: Optional[Path] = None,
    ) -> bool:
        w = self.window
        if not w._has_open_doc():
            return False

        replacement = self._normalize_simple_text_input(new_text)
        if (
            replacement == target.text
            and bool(force_italic) == bool(target.is_italic)
            and bool(force_bold) == bool(target.is_bold)
            and not draw_vector_arrow
            and preferred_font_path is None
        ):
            return True

        if not allow_overflow_right:
            return self._apply_simple_text_reflow_replace(
                target,
                replacement,
                force_italic=force_italic,
                force_bold=force_bold,
                draw_vector_arrow=draw_vector_arrow,
                super_shift_em=super_shift_em,
                sub_shift_em=sub_shift_em,
                vector_shift_em=vector_shift_em,
                preferred_font_path=preferred_font_path,
            )

        target_plan = self._resolve_insert_font_plan(
            replacement,
            target.font_name,
            prefer_times_new_roman=allow_overflow_right,
            force_italic=force_italic,
            force_bold=force_bold,
            preferred_font_path=preferred_font_path,
            page_index=target.page_index,
        )
        width = self._estimate_scripted_text_width(
            replacement,
            target.font_name,
            target.font_size,
            force_italic=force_italic,
            force_bold=force_bold,
            preferred_font_path=preferred_font_path,
            resolved_plan=target_plan,
        )
        edit_rect = self._effective_edit_rect(target, allow_overflow_right)
        if not self._is_safe_edit_rect(target, edit_rect):
            QMessageBox.warning(
                w,
                "안전 편집 불가",
                "선택 영역이 비어 있어 수정할 수 없습니다."
            )
            return False
        if (not allow_overflow_right) and replacement and width is not None:
            max_width = float(edit_rect.width) * 1.10
            if width > max_width:
                ret = QMessageBox.question(
                    w,
                    "텍스트 폭 초과",
                    f"입력한 텍스트가 선택 영역보다 {int((width / float(edit_rect.width) - 1) * 100)}% 깁니다.\n그래도 적용하시겠습니까?",
                    QMessageBox.Yes | QMessageBox.No,
                )
                if ret != QMessageBox.Yes:
                    return False

        w._push_undo_snapshot()

        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
        tmp_path = Path(tmp.name)
        tmp.close()
        w._register_temp_file(tmp_path)

        src_doc = None
        try:
            src_doc = fitz.open(str(w.base_path))
            page = src_doc[target.page_index]

            erase_rect = fitz.Rect(edit_rect)
            self._erase_text_rects(page, [(erase_rect, self._rgb_from_span_color(target.color))])

            if replacement:
                rgb = self._rgb_from_span_color(target.color)
                inserted = self._insert_simple_text_with_fallback(
                    page=page,
                    origin=target.origin,
                    text=replacement,
                    font_name=target.font_name,
                    font_size=target.font_size,
                    color=rgb,
                    prefer_times_new_roman=allow_overflow_right,
                    target_rect=edit_rect,
                    force_italic=force_italic,
                    force_bold=force_bold,
                    super_shift_em=super_shift_em,
                    sub_shift_em=sub_shift_em,
                    preferred_font_path=preferred_font_path,
                )
                if not inserted:
                    raise RuntimeError("텍스트 삽입 실패")
                if draw_vector_arrow:
                    self._draw_vector_arrow_in_rect(
                        page=page,
                        target_rect=edit_rect,
                        color=rgb,
                        base_size=target.font_size,
                        text_width=width,
                        baseline_y=float(target.origin.y),
                        vector_shift_em=vector_shift_em,
                    )

            src_doc.save(str(tmp_path))
            src_doc.close()
            src_doc = None

            saved_h = w.view.horizontalScrollBar().value()
            saved_v = w.view.verticalScrollBar().value()

            old_base = w.base_path
            old_temp = w.temp_margin_file
            w._activate_temp_pdf(tmp_path, old_base, old_temp)

            w.current_page_index = min(target.page_index, len(w.doc) - 1)
            w._thumbnail_selected_pages = {w.current_page_index}
            w._clear_search_state()
            w._mark_modified()
            w._refresh_thumbnail_sidebar(force=True)
            w._scroll_to_current_after_render = False
            w.render_page()
            w.view.horizontalScrollBar().setValue(saved_h)
            w.view.verticalScrollBar().setValue(saved_v)
            w.statusBar().showMessage("텍스트를 적용했습니다.", 1800)
            return True

        except Exception as e:
            if src_doc is not None:
                try:
                    src_doc.close()
                except Exception:
                    pass
            w._cleanup_temp_file(tmp_path)
            if w.undo_stack:
                w.undo_stack.pop()
            QMessageBox.critical(w, "텍스트 수정 실패", f"오류:\n{e}")
            return False

    def edit_text_at_point(self, click_point: fitz.Point, page_index: Optional[int] = None, select_line: bool = False) -> bool:
        w = self.window
        if not w.doc:
            return False

        target_page_idx = w.current_page_index if page_index is None else page_index
        target = self._find_text_target_at_point(target_page_idx, click_point, select_line=select_line)
        if target is None:
            return False

        action = run_text_edit_dialog(w, target, select_line, self.font_dialog_options(target))
        if action is None:
            return True

        font_key = str(action.get("font_choice", "__auto__") or "__auto__")
        preferred_font_path = None if font_key == "__auto__" else Path(font_key)

        self._apply_simple_text_replace(
            target,
            action["text"],
            allow_overflow_right=select_line,
            force_italic=bool(action["force_italic"]),
            force_bold=bool(action["force_bold"]),
            draw_vector_arrow=bool(action["draw_vector_arrow"]),
            super_shift_em=float(action.get("super_shift_em", 0.40)),
            sub_shift_em=float(action.get("sub_shift_em", 0.16)),
            vector_shift_em=float(action.get("vector_shift_em", 0.80)),
            preferred_font_path=preferred_font_path,
        )
        return True
