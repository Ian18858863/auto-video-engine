from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat
from typing import Any

from app.core.config import resolve_configured_path
from app.materials.asset_paths import AutoMaterialPathError, AutoMaterialPaths


_VERSION_PATTERN = re.compile(
    r"^version\s*:\s*['\"]?([A-Za-z0-9][A-Za-z0-9_.+\-]{0,63})['\"]?\s*$",
    re.MULTILINE,
)
@dataclass(frozen=True, slots=True)
class SkillDefinition:
    id: str
    relative_directory: str
    name: str
    purpose: str
    applicable_route: str
    affected_modules: tuple[str, ...]
    fallback: str
    cost_class: str


_SKILLS = (
    SkillDefinition(
        id="hyperframes",
        relative_directory="hyperframes",
        name="HyperFrames",
        purpose="HTML 场景动画与视频渲染候选",
        applicable_route="剪辑 -> 成片（同题试点通过后才可接主线）",
        affected_modules=("剪辑", "成片"),
        fallback="当前 TimelineSpec + FFmpeg 本地草稿链",
        cost_class="本地工具；外部素材费用另计",
    ),
    SkillDefinition(
        id="video-use",
        relative_directory="video-use",
        name="video-use",
        purpose="已有视频的转写、剪裁、字幕和通用编辑",
        applicable_route="素材 -> 剪辑（条件接入）",
        affected_modules=("素材", "剪辑"),
        fallback="现有 FFmpeg 兼容编辑链",
        cost_class="本地工具；模型依赖可能产生费用",
    ),
    SkillDefinition(
        id="chengfeng-talking-head",
        relative_directory="chengfeng-videocut-skills/剪口播",
        name="剪口播",
        purpose="中文真人口播的转写、口误确认、粗剪与字幕校对",
        applicable_route="素材 -> 剪辑（真人口播专用）",
        affected_modules=("素材", "剪辑"),
        fallback="video-use 或现有人工确认链",
        cost_class="本地工具；外部转写 Key 另计",
    ),
    SkillDefinition(
        id="muapi-media-generation",
        relative_directory="muapi-media-generation",
        name="MuAPI Media Generation",
        purpose="缺口图片、视频和音频生成",
        applicable_route="素材（有预算时补位，不作为必需依赖）",
        affected_modules=("素材",),
        fallback="自有素材、图库候选或信息动效",
        cost_class="外部付费服务",
    ),
    SkillDefinition(
        id="seedance-prompt-zh",
        relative_directory="seedance-prompt-zh",
        name="Seedance 提示词",
        purpose="生成多模态视频提示词，不负责调用生成服务",
        applicable_route="素材（提示词辅助）",
        affected_modules=("素材",),
        fallback="项目内人工提示词",
        cost_class="提示词方法；生成服务费用另计",
    ),
)


def build_skill_statuses(skills_root: Path | str) -> list[dict[str, Any]]:
    try:
        root = resolve_configured_path(skills_root)
    except (OSError, TypeError, ValueError):
        return [_blocked_skill(definition) for definition in _SKILLS]

    statuses: list[dict[str, Any]] = []
    for definition in _SKILLS:
        skill_file = root / Path(definition.relative_directory) / "SKILL.md"
        installed = _observe_regular_file(skill_file)
        if installed is None:
            status = "blocked_observation"
            configured: bool | None = None
            reason = "无法读取该 Skill 的安装证据，未把未知状态写成未安装"
        elif installed:
            status = "installed_not_integrated"
            configured = False
            reason = "已安装，但尚未接入产品适配器并通过真实黄金项目验证"
        else:
            status = "not_installed"
            configured = False
            reason = "未在服务端批准的 Skills 根目录找到 SKILL.md"
        statuses.append(
            {
                "id": definition.id,
                "name": definition.name,
                "status": status,
                "installed": installed,
                "configured": configured,
                "verified": False,
                "production_allowed": False,
                "declared_version": _declared_version(skill_file) if installed is True else "",
                "last_verified_at": None,
                "purpose": definition.purpose,
                "applicable_route": definition.applicable_route,
                "affected_modules": list(definition.affected_modules),
                "fallback": definition.fallback,
                "cost_class": definition.cost_class,
                "reason": reason,
            }
        )
    return statuses


def build_provider_statuses(
    settings: Any,
    current_ai_status: dict[str, Any],
) -> list[dict[str, Any]]:
    fake = str(current_ai_status.get("name", "")).casefold() == "fake"
    ai_status = str(current_ai_status.get("status", "blocked_unknown_provider"))
    ai_installed = ai_status != "blocked_unknown_provider"
    ai_configured = ai_status in {"configured_unverified", "verified"}
    ai_verified = bool(current_ai_status.get("verified"))
    ai_production_allowed = bool(current_ai_status.get("production_allowed"))
    if not (ai_installed and ai_configured and ai_verified) or fake:
        ai_production_allowed = False
    ai_entry = {
        "id": "script_ai",
        "name": f"脚本 AI（{current_ai_status.get('name', 'unknown')}）",
        "status": ai_status,
        "installed": ai_installed,
        "configured": ai_configured,
        "verified": ai_verified,
        "production_allowed": ai_production_allowed,
        "secret_configured": bool(current_ai_status.get("secret_configured")),
        "declared_version": str(getattr(settings, "ai_model", "") or ""),
        "last_verified_at": None,
        "purpose": "选题和脚本生成",
        "applicable_route": "选题 -> 脚本",
        "affected_modules": ["选题", "脚本"],
        "fallback": (
            "无生产级 fallback"
            if fake
            else "fake 仅限隔离测试，不可作为生产结果"
        ),
        "cost_class": "外部模型调用；费用与配额以所选 provider 为准",
        "reason": str(current_ai_status.get("reason", "")),
    }

    material_paths: AutoMaterialPaths | None
    try:
        material_paths = AutoMaterialPaths(
            root=resolve_configured_path(settings.auto_material_root_dir)
        )
    except (OSError, TypeError, ValueError):
        material_paths = None
    return [
        ai_entry,
        _stock_provider(
            provider_id="pexels",
            name="Pexels",
            paths=material_paths,
            key_reader="pexels",
            fallback="Pixabay -> 自有素材 -> 信息动效",
        ),
        _stock_provider(
            provider_id="pixabay",
            name="Pixabay",
            paths=material_paths,
            key_reader="pixabay",
            fallback="Pexels -> 自有素材 -> 信息动效",
        ),
        _tts_provider(settings),
    ]


def _stock_provider(
    *,
    provider_id: str,
    name: str,
    paths: AutoMaterialPaths | None,
    key_reader: str,
    fallback: str,
) -> dict[str, Any]:
    if paths is None:
        configured: bool | None = None
        status = "blocked_path_policy"
        reason = "生产素材根目录未通过 D 盘路径策略，无法判断 Key 状态"
    else:
        try:
            if key_reader == "pexels":
                paths.read_pexels_api_key()
            else:
                paths.read_pixabay_api_key()
        except (AutoMaterialPathError, OSError, UnicodeError, ValueError):
            configured = False
            status = "blocked_missing_config"
            reason = "生产素材执行链未读取到可用 Key；不会发起 provider 请求"
        else:
            configured = True
            status = "configured_unverified"
            reason = "生产素材执行链能读取 Key，但本次只读检查没有联网验证"
    return {
        "id": provider_id,
        "name": name,
        "status": status,
        "installed": True,
        "configured": configured,
        "verified": False,
        "production_allowed": False,
        "secret_configured": configured is True,
        "declared_version": "",
        "last_verified_at": None,
        "purpose": "普通镜头的图库候选来源",
        "applicable_route": "素材（普通镜头候选）",
        "affected_modules": ["素材"],
        "fallback": fallback,
        "cost_class": "外部服务；费用与配额以 provider 账户为准",
        "reason": reason,
    }


def _tts_provider(settings: Any) -> dict[str, Any]:
    selected = str(getattr(settings, "tts_provider", "")).casefold() == "windows_sapi"
    installed = os.name == "nt"
    if not selected:
        status = "disabled"
        reason = "当前未选择 Windows SAPI"
    elif not installed:
        status = "blocked_missing_dependency"
        reason = "当前进程不是 Windows，无法使用 Windows SAPI"
    else:
        status = "installed_unverified"
        reason = "Windows 依赖存在，但本次只读检查未生成试听文件"
    return {
        "id": "windows_sapi",
        "name": "Windows SAPI",
        "status": status,
        "installed": installed,
        "configured": selected,
        "verified": False,
        "production_allowed": False,
        "secret_configured": False,
        "declared_version": "随 Windows",
        "last_verified_at": None,
        "purpose": "本地中文 TTS fallback",
        "applicable_route": "剪辑 -> 成片（本地 TTS fallback）",
        "affected_modules": ["剪辑", "成片"],
        "fallback": "保留无配音草稿并阻止正式批准",
        "cost_class": "本地能力",
        "reason": reason,
    }


def _declared_version(skill_file: Path) -> str:
    try:
        prefix = skill_file.read_text(encoding="utf-8")[:4096]
    except (OSError, UnicodeError):
        return ""
    match = _VERSION_PATTERN.search(prefix)
    return match.group(1) if match else ""


def _observe_regular_file(path: Path) -> bool | None:
    try:
        return stat.S_ISREG(path.stat().st_mode)
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        return None


def _blocked_skill(definition: SkillDefinition) -> dict[str, Any]:
    return {
        "id": definition.id,
        "name": definition.name,
        "status": "blocked_path_policy",
        "installed": None,
        "configured": None,
        "verified": False,
        "production_allowed": False,
        "declared_version": "",
        "last_verified_at": None,
        "purpose": definition.purpose,
        "applicable_route": definition.applicable_route,
        "affected_modules": list(definition.affected_modules),
        "fallback": definition.fallback,
        "cost_class": definition.cost_class,
        "reason": "Skills 根目录未通过 D 盘只读路径策略",
    }
