from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from zipfile import BadZipFile, ZipFile
import re
from typing import Callable


class AutoMaterialPathError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AutoMaterialPaths:
    root: Path = Path("D:/素材")

    @property
    def backup_dir(self) -> Path:
        return self.root / "备用"

    @property
    def auto_download_dir(self) -> Path:
        return self.root / "自动下载"

    @property
    def pexels_root(self) -> Path:
        return self.auto_download_dir / "Pexels"

    @property
    def pixabay_root(self) -> Path:
        return self.auto_download_dir / "Pixabay"

    @property
    def pixabay_video_dir(self) -> Path:
        return self.pixabay_root / "视频"

    @property
    def pixabay_photo_dir(self) -> Path:
        return self.pixabay_root / "照片"

    @property
    def coverr_root(self) -> Path:
        return self.auto_download_dir / "Coverr"

    @property
    def coverr_video_dir(self) -> Path:
        return self.coverr_root / "视频"

    @property
    def pexels_video_dir(self) -> Path:
        return self.pexels_root / "视频"

    @property
    def pexels_photo_dir(self) -> Path:
        return self.pexels_root / "照片"

    @property
    def official_page_dir(self) -> Path:
        return self.auto_download_dir / "官方页面"

    @property
    def generated_motion_dir(self) -> Path:
        return self.root / "自动生成"

    @property
    def usage_registry_path(self) -> Path:
        return self.root / "素材使用记录.json"

    @property
    def fingerprint_registry_path(self) -> Path:
        return self.root / "素材指纹.json"

    @property
    def pexels_api_key_path(self) -> Path:
        return self.pexels_root / "API Key"

    @property
    def pixabay_api_key_path(self) -> Path:
        return self.pixabay_root / "API Key"

    def ensure_directories(
        self,
        drive_exists: Callable[[str], bool] | None = None,
    ) -> None:
        if _windows_drive(self.root) == "C":
            raise AutoMaterialPathError("自动素材根目录不能配置到 C 盘。")
        if _windows_drive(self.root) == "D":
            checker = drive_exists or _drive_exists
            if not checker("D"):
                raise AutoMaterialPathError("D 盘不存在，请先确认 D:/素材 目录。")
        for directory in [
            self.backup_dir,
            self.auto_download_dir,
            self.pexels_root,
            self.pexels_video_dir,
            self.pexels_photo_dir,
            self.pixabay_root,
            self.pixabay_video_dir,
            self.pixabay_photo_dir,
            self.coverr_root,
            self.coverr_video_dir,
            self.official_page_dir,
            self.generated_motion_dir,
        ]:
            directory.mkdir(parents=True, exist_ok=True)

    def read_pexels_api_key(self) -> str:
        key_path = self._resolve_pexels_api_key_path()
        raw_key = _read_key_file_text(key_path)
        key = _normalize_api_key_text(raw_key)
        if not key:
            raise AutoMaterialPathError(f"Pexels API Key 文件为空：{key_path}")
        return key

    def _resolve_pexels_api_key_path(self) -> Path:
        direct_candidates = [
            self.pexels_api_key_path,
            self.pexels_root / "API Key.txt",
            self.pexels_root / "API Key.env",
        ]
        for candidate in direct_candidates:
            if candidate.is_file():
                return candidate
        if self.pexels_api_key_path.is_dir():
            folder_candidates = [
                self.pexels_api_key_path / "API Key",
                self.pexels_api_key_path / "API Key.txt",
                self.pexels_api_key_path / "API Key.env",
                self.pexels_api_key_path / "API Key.docx",
            ]
            for candidate in folder_candidates:
                if candidate.is_file():
                    return candidate
            for candidate in self.pexels_api_key_path.iterdir():
                if candidate.is_file() and candidate.suffix.lower() in {"", ".txt", ".env", ".docx"}:
                    return candidate
        raise AutoMaterialPathError(
            f"未找到 Pexels API Key，请把 Key 放到批准的 D 盘路径：{self.pexels_api_key_path}。"
        )

    def read_pixabay_api_key(self) -> str:
        key_path = self._resolve_pixabay_api_key_path()
        raw_key = _read_key_file_text(key_path)
        key = _normalize_api_key_text(raw_key)
        if not key:
            raise AutoMaterialPathError(f"Pixabay API Key 文件为空：{key_path}")
        return key

    def _resolve_pixabay_api_key_path(self) -> Path:
        direct_candidates = [
            self.pixabay_api_key_path,
            self.pixabay_root / "API Key.txt",
            self.pixabay_root / "API Key.env",
            self.pixabay_root / "Pixabay API",
            self.pixabay_root / "Pixabay API.txt",
            self.pixabay_root / "Pixabay API.env",
        ]
        for candidate in direct_candidates:
            if candidate.is_file():
                return candidate
        if self.pixabay_api_key_path.is_dir():
            folder_candidates = [
                self.pixabay_api_key_path / "API Key",
                self.pixabay_api_key_path / "API Key.txt",
                self.pixabay_api_key_path / "API Key.env",
                self.pixabay_api_key_path / "API Key.docx",
                self.pixabay_api_key_path / "Pixabay API",
                self.pixabay_api_key_path / "Pixabay API.txt",
                self.pixabay_api_key_path / "Pixabay API.env",
                self.pixabay_api_key_path / "Pixabay API.docx",
            ]
            for candidate in folder_candidates:
                if candidate.is_file():
                    return candidate
            for candidate in self.pixabay_api_key_path.iterdir():
                if candidate.is_file() and candidate.suffix.lower() in {"", ".txt", ".env", ".docx"}:
                    return candidate

        raise AutoMaterialPathError(
            f"未找到 Pixabay API Key，请把 Key 放到批准的 D 盘路径：{self.pixabay_api_key_path}。"
        )


def _windows_drive(path: Path) -> str | None:
    normalized = str(path).replace("\\", "/")
    if len(normalized) >= 3 and normalized[1:3] == ":/":
        return normalized[0].upper()
    return None


def _drive_exists(drive: str) -> bool:
    return Path(f"{drive}:/").exists()


def _normalize_api_key_text(value: str) -> str:
    if "=" not in value:
        return value.strip()
    for line in value.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, raw_value = stripped.split("=", 1)
        if key.strip().upper() in {"PEXELS_API_KEY", "PIXABAY_API_KEY", "API_KEY", "KEY"}:
            return raw_value.strip().strip('"').strip("'")
    return value.strip()


def _read_key_file_text(path: Path) -> str:
    if path.suffix.lower() == ".docx":
        try:
            with ZipFile(path) as archive:
                document_xml = archive.read("word/document.xml").decode("utf-8", errors="ignore")
        except (BadZipFile, KeyError, OSError) as exc:
            raise AutoMaterialPathError(f"Pexels API Key docx 文件无法读取：{path}") from exc
        text = re.sub(r"<[^>]+>", "", document_xml)
        return text.strip()
    return path.read_text(encoding="utf-8").strip()
