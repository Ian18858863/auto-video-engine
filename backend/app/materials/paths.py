from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable


ROOT_DIRS = [
    "项目",
    "exports",
    "归档",
    "备选素材",
]

PROJECT_DIRS = [
    "Pexels素材",
    "官方录屏",
    "信息动效",
    "全部素材",
    "素材信息",
]


class MaterialPathError(RuntimeError):
    pass


@dataclass(slots=True)
class ProjectMaterialPaths:
    root: Path = Path("D:/短视频工厂")
    project_name: str = "未命名项目"
    project_key: str | None = None
    project_root_dir: Path | None = None

    @property
    def material_library_dir(self) -> Path:
        # 兼容旧调用名；现在不再单独创建“素材库”，全局备用素材放这里。
        return self.backup_material_dir

    @property
    def export_dir(self) -> Path:
        return self.root / "exports"

    @property
    def archive_dir(self) -> Path:
        return self.root / "归档"

    @property
    def backup_material_dir(self) -> Path:
        return self.root / "备选素材"

    @property
    def project_root(self) -> Path:
        return self.project_root_dir or self.root / "项目"

    @property
    def project_dir(self) -> Path:
        # Pipeline callers pass the immutable key. project_name remains only
        # for legacy/non-pipeline tools that have no persistent pipeline id.
        return self.project_root / (self.project_key or self.project_name)

    @property
    def jianying_download_dir(self) -> Path:
        return self.backup_material_dir / "旧剪映下载"

    @property
    def official_evidence_dir(self) -> Path:
        # 兼容旧属性名；新目录名按 Ian 当前规则叫“官方录屏”。
        return self.official_recording_dir

    @property
    def official_recording_dir(self) -> Path:
        return self.project_dir / "官方录屏"

    @property
    def pexels_material_dir(self) -> Path:
        return self.project_dir / "Pexels素材"

    @property
    def info_motion_dir(self) -> Path:
        return self.project_dir / "信息动效"

    @property
    def all_materials_dir(self) -> Path:
        return self.project_dir / "全部素材"

    @property
    def local_library_dir(self) -> Path:
        return self.backup_material_dir

    def ensure_directories(
        self,
        drive_exists: Callable[[str], bool] | None = None,
    ) -> None:
        self.ensure_shared_directories(drive_exists=drive_exists)
        if self.project_key is not None:
            if not self.project_dir.is_dir():
                raise MaterialPathError(
                    f"项目身份目录尚未准备或已被替换：{self.project_dir}"
                )
            missing = [
                dirname
                for dirname in PROJECT_DIRS
                if not (self.project_dir / dirname).is_dir()
            ]
            if missing:
                raise MaterialPathError(
                    f"项目身份子目录尚未安全准备：{', '.join(missing)}"
                )
            return

        self.project_dir.mkdir(parents=True, exist_ok=True)
        for dirname in PROJECT_DIRS:
            (self.project_dir / dirname).mkdir(parents=True, exist_ok=True)

    def ensure_shared_directories(
        self,
        drive_exists: Callable[[str], bool] | None = None,
    ) -> None:
        if _is_c_drive_path(self.root):
            raise MaterialPathError("素材根目录不能配置到 C 盘；请手动配置其他磁盘或使用项目内导出目录。")
        if _is_d_drive_path(self.root):
            checker = drive_exists or _drive_exists
            if not checker("D"):
                raise MaterialPathError("D 盘不存在，请先手动配置素材根目录；系统不会自动改到 C 盘。")

        root_dirs = ROOT_DIRS
        if (
            self.project_root_dir is not None
            and self.project_root_dir.resolve(strict=False)
            != (self.root / "项目").resolve(strict=False)
        ):
            root_dirs = [dirname for dirname in ROOT_DIRS if dirname != "项目"]

        for dirname in root_dirs:
            (self.root / dirname).mkdir(parents=True, exist_ok=True)


def resolve_portable_material_root(
    configured_root: Path,
    *,
    fallback_root: Path = Path("D:/短视频工厂"),
    drive_exists: Callable[[str], bool] | None = None,
) -> Path:
    drive = _windows_drive(configured_root)
    if drive == "C":
        return fallback_root
    if drive and not (drive_exists or _drive_exists)(drive):
        return fallback_root
    return configured_root


def _is_c_drive_path(path: Path) -> bool:
    return _windows_drive(path) == "C"


def _is_d_drive_path(path: Path) -> bool:
    return _windows_drive(path) == "D"


def _windows_drive(path: Path) -> str | None:
    normalized = str(path).replace("\\", "/")
    if len(normalized) >= 3 and normalized[1:3] == ":/":
        return normalized[0].upper()
    return None


def _drive_exists(drive: str) -> bool:
    return Path(f"{drive}:/").exists()
