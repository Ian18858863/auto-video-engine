from __future__ import annotations

import argparse
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


_REPO_MARKERS = ("backend", "frontend", "PROJECT_BRIEF.md")
_DEFAULT_SYSTEM_DRIVE = os.environ.get("SystemDrive") or "C:"


@dataclass(frozen=True, slots=True)
class RuntimeIdentity:
    invoked_root: Path
    physical_repo_root: Path
    canonical_database_path: Path
    is_alias: bool


def require_non_system_drive(
    path: Path,
    system_drive: str = _DEFAULT_SYSTEM_DRIVE,
) -> Path:
    candidate = Path(path)
    drive = candidate.drive.upper()
    configured_system_drive = system_drive.strip().rstrip("\\/").upper()

    if (
        not candidate.is_absolute()
        or len(drive) != 2
        or drive[1] != ":"
        or not drive[0].isalpha()
    ):
        raise ValueError(f"路径必须是带盘符的绝对路径：{candidate}")
    if len(configured_system_drive) != 2 or configured_system_drive[1] != ":":
        raise ValueError(f"系统盘配置无效：{system_drive}")
    if drive == configured_system_drive:
        raise ValueError(f"禁止写入系统盘 {configured_system_drive}：{candidate}")
    return candidate


def resolve_runtime_identity(start_path: Path | None = None) -> RuntimeIdentity:
    invoked_path = _absolute_without_resolving(start_path or Path.cwd())
    invoked_root = _find_invoked_repo_root(invoked_path)
    physical_repo_root = _git_toplevel(invoked_root) or invoked_root.resolve()

    if not _has_repo_markers(physical_repo_root):
        raise RuntimeError(f"物理 Git 根目录缺少项目标记：{physical_repo_root}")

    require_non_system_drive(physical_repo_root)
    canonical_database_path = physical_repo_root / "trend_video_factory.db"
    return RuntimeIdentity(
        invoked_root=invoked_root,
        physical_repo_root=physical_repo_root,
        canonical_database_path=canonical_database_path,
        is_alias=_normalized_path(invoked_root) != _normalized_path(physical_repo_root),
    )


def _absolute_without_resolving(path: Path) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    return Path(os.path.abspath(candidate))


def _find_invoked_repo_root(start_path: Path) -> Path:
    candidate = start_path.parent if start_path.is_file() else start_path
    for current in (candidate, *candidate.parents):
        if current.name.casefold() == "auto video engine".casefold():
            raise RuntimeError(
                "Auto Video Engine 根目录是独立研究工作区，不是可执行项目别名。"
            )
        if _has_repo_markers(current):
            return current
    raise RuntimeError(f"找不到项目根目录：{start_path}")


def _has_repo_markers(path: Path) -> bool:
    return (
        (path / _REPO_MARKERS[0]).is_dir()
        and (path / _REPO_MARKERS[1]).is_dir()
        and (path / _REPO_MARKERS[2]).is_file()
    )


def _git_toplevel(candidate: Path) -> Path | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(candidate), "rev-parse", "--show-toplevel"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return _absolute_without_resolving(Path(result.stdout.strip()))


def _normalized_path(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Resolve the executable repository root.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--print-root", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)

    identity = resolve_runtime_identity()
    if args.print_root:
        print(identity.physical_repo_root)
        return 0

    print(f"Invoked root: {identity.invoked_root}")
    print(f"Physical repository root: {identity.physical_repo_root}")
    print(f"Canonical database: {identity.canonical_database_path}")
    print(f"Alias: {str(identity.is_alias).lower()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
