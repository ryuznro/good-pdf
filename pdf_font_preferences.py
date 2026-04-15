"""사용자 폰트 환경설정 (최근 사용 폰트, PDF별 폰트 별칭) 영구 저장.

저장 위치:
- macOS:   ~/Library/Preferences/good-pdf/font_preferences.json
- Linux:   ~/.config/good-pdf/font_preferences.json
- Windows: %APPDATA%/good-pdf/font_preferences.json

스키마:
{
    "recent_fonts": ["/path/to/font1.ttf", ...],   # 최대 MAX_RECENT 개
    "pinned_font":  "/path/to/font.ttf" | null,     # 전역 기본 폰트
    "document_aliases": {
        "<pdf_path>": {
            "<original_font_name>": "/path/to/chosen_font.ttf"
        }
    }
}

우선순위: document_aliases > pinned_font > 자동 감지.

- pinned_font: 사용자가 "앞으로 이 폰트를 기본값으로" 체크한 폰트. 새 PDF나
  별칭이 없는 span 에서 자동 감지 대신 이 폰트가 미리 선택됨.
- document_aliases: 특정 PDF 에서 특정 원본 폰트 이름이 잘못 감지될 때 보정한
  매핑. pinned_font 보다 우선함.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional

from PySide6.QtCore import QStandardPaths

MAX_RECENT = 5


class FontPreferences:
    def __init__(self) -> None:
        base = QStandardPaths.writableLocation(QStandardPaths.GenericConfigLocation)
        if not base:
            base = str(Path.home() / ".config")
        self._dir = Path(base) / "good-pdf"
        self._path = self._dir / "font_preferences.json"
        self._data: Dict = {
            "recent_fonts": [],
            "pinned_font": None,
            "document_aliases": {},
        }
        self._load()

    # ---- persistence -----------------------------------------------------
    def _load(self) -> None:
        try:
            if not self._path.exists():
                return
            with self._path.open("r", encoding="utf-8") as f:
                loaded = json.load(f)
            if not isinstance(loaded, dict):
                return
            recent = loaded.get("recent_fonts")
            if isinstance(recent, list):
                self._data["recent_fonts"] = [str(x) for x in recent if isinstance(x, str)]
            pinned = loaded.get("pinned_font")
            if isinstance(pinned, str) and pinned:
                self._data["pinned_font"] = pinned
            aliases = loaded.get("document_aliases")
            if isinstance(aliases, dict):
                cleaned: Dict[str, Dict[str, str]] = {}
                for pdf, mapping in aliases.items():
                    if not isinstance(pdf, str) or not isinstance(mapping, dict):
                        continue
                    cleaned[pdf] = {
                        str(k): str(v)
                        for k, v in mapping.items()
                        if isinstance(k, str) and isinstance(v, str)
                    }
                self._data["document_aliases"] = cleaned
        except Exception:
            # 손상된 파일 등은 조용히 초기화
            self._data = {
                "recent_fonts": [],
                "pinned_font": None,
                "document_aliases": {},
            }

    def _save(self) -> None:
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            tmp.replace(self._path)
        except Exception:
            pass

    # ---- pinned font (global default) ------------------------------------
    def get_pinned(self) -> Optional[str]:
        value = self._data.get("pinned_font")
        return value if isinstance(value, str) and value else None

    def set_pinned(self, font_path: Optional[str]) -> None:
        if not font_path or font_path == "__auto__":
            self.clear_pinned()
            return
        if self._data.get("pinned_font") == font_path:
            return
        self._data["pinned_font"] = font_path
        self._save()

    def clear_pinned(self) -> None:
        if self._data.get("pinned_font") is None:
            return
        self._data["pinned_font"] = None
        self._save()

    # ---- recent fonts ----------------------------------------------------
    def get_recent(self) -> List[str]:
        return list(self._data.get("recent_fonts", []))

    def add_recent(self, font_path: Optional[str]) -> None:
        if not font_path or font_path == "__auto__":
            return
        recent = self._data.setdefault("recent_fonts", [])
        if font_path in recent:
            recent.remove(font_path)
        recent.insert(0, font_path)
        del recent[MAX_RECENT:]
        self._save()

    # ---- document aliases ------------------------------------------------
    @staticmethod
    def _norm_pdf_key(pdf_path: Optional[str]) -> Optional[str]:
        if not pdf_path:
            return None
        try:
            return str(Path(pdf_path).expanduser().resolve())
        except Exception:
            return str(pdf_path)

    def get_alias(
        self,
        pdf_path: Optional[str],
        original_font_name: Optional[str],
    ) -> Optional[str]:
        key = self._norm_pdf_key(pdf_path)
        if not key or not original_font_name:
            return None
        return (
            self._data.get("document_aliases", {})
            .get(key, {})
            .get(original_font_name)
        )

    def set_alias(
        self,
        pdf_path: Optional[str],
        original_font_name: Optional[str],
        font_path: Optional[str],
    ) -> None:
        key = self._norm_pdf_key(pdf_path)
        if not key or not original_font_name or not font_path or font_path == "__auto__":
            return
        aliases = self._data.setdefault("document_aliases", {})
        mapping = aliases.setdefault(key, {})
        if mapping.get(original_font_name) == font_path:
            return  # 변경 없으면 디스크 쓰기 생략
        mapping[original_font_name] = font_path
        self._save()

    def clear_alias(
        self,
        pdf_path: Optional[str],
        original_font_name: Optional[str] = None,
    ) -> None:
        key = self._norm_pdf_key(pdf_path)
        if not key:
            return
        aliases = self._data.get("document_aliases", {})
        if key not in aliases:
            return
        if original_font_name is None:
            aliases.pop(key, None)
        else:
            aliases.get(key, {}).pop(original_font_name, None)
            if not aliases.get(key):
                aliases.pop(key, None)
        self._save()
