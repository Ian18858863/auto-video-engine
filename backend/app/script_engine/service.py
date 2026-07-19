from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.copywriting_samples.schemas import CopywritingSamplePayload
from app.copywriting_samples.service import (
    CopywritingSampleListResult,
    CopywritingSampleNotFoundError,
    create_copywriting_sample,
    delete_copywriting_sample,
    get_copywriting_sample,
    get_copywriting_sample_context,
    list_copywriting_samples,
    serialize_copywriting_sample,
    serialize_copywriting_sample_list,
    update_copywriting_sample,
)
from app.models import CopywritingSample


SCRIPT_ENGINE_REFERENCE_RULE = (
    "脚本引擎只学习结构、节奏、转译方式和表达方法，不直接复制原文；"
    "如果和账号风格库冲突，以账号风格库为准。"
)


ScriptEngineSampleNotFoundError = CopywritingSampleNotFoundError


def create_script_engine_sample(
    session: Session,
    payload: CopywritingSamplePayload,
) -> CopywritingSample:
    return create_copywriting_sample(session, payload)


def update_script_engine_sample(
    session: Session,
    sample_id: int,
    payload: CopywritingSamplePayload,
) -> CopywritingSample:
    return update_copywriting_sample(session, sample_id, payload)


def delete_script_engine_sample(session: Session, sample_id: int) -> None:
    delete_copywriting_sample(session, sample_id)


def get_script_engine_sample(session: Session, sample_id: int) -> CopywritingSample:
    return get_copywriting_sample(session, sample_id)


def list_script_engine_samples(
    session: Session,
    *,
    sample_type: str | None = None,
    scene: str | None = None,
    content_direction: str | None = None,
    tag: str | None = None,
    keyword: str | None = None,
    enabled_only: bool = False,
    key_reference_only: bool = False,
    page: int = 1,
    page_size: int = 20,
) -> CopywritingSampleListResult:
    return list_copywriting_samples(
        session,
        sample_type=sample_type,
        scene=scene,
        content_direction=content_direction,
        tag=tag,
        keyword=keyword,
        enabled_only=enabled_only,
        key_reference_only=key_reference_only,
        page=page,
        page_size=page_size,
    )


def get_script_engine_context(
    session: Session,
    *,
    limit: int = 6,
) -> dict[str, Any]:
    context = dict(get_copywriting_sample_context(session, limit=limit))
    context["source"] = "script_engine"
    context["reference_rule"] = SCRIPT_ENGINE_REFERENCE_RULE
    context["account_style_priority"] = (
        "账号风格库是最高优先级；脚本引擎只负责结构、节奏、样本参考和脚本方法。"
    )
    context["storage_compatibility"] = (
        "当前兼容复用 copywriting_samples 表；新流程统一从 script_engine 读取。"
    )
    return context


def serialize_script_engine_sample(item: CopywritingSample) -> dict[str, Any]:
    data = serialize_copywriting_sample(item)
    data["engine"] = "script_engine"
    return data


def serialize_script_engine_sample_list(result: CopywritingSampleListResult) -> dict[str, Any]:
    data = serialize_copywriting_sample_list(result)
    data["items"] = [serialize_script_engine_sample(item) for item in result.items]
    data["reference_rule"] = SCRIPT_ENGINE_REFERENCE_RULE
    data["style_priority"] = "账号风格库优先；脚本引擎只做结构和表达参考。"
    data["storage_compatibility"] = "当前复用旧表，后续稳定后再废除旧接口。"
    data["engine"] = "script_engine"
    data["real_ai_called"] = False
    return data
