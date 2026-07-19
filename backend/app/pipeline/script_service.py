from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.account_style.service import get_account_style_context
from app.ai.providers.base import AIProvider
from app.ai.schemas import AIRequest, GenerationType
from app.ai.service import generate_with_cache
from app.models import ScriptAsset, TopicIdea
from app.script_engine.service import get_script_engine_context


@dataclass(slots=True)
class ScriptGenerationResult:
    script_asset: ScriptAsset
    from_cache: bool
    input_hash: str


def generate_script_for_topic(
    session: Session,
    topic_idea: TopicIdea,
    provider: AIProvider,
) -> ScriptGenerationResult:
    account_style = get_account_style_context(session)
    script_engine = get_script_engine_context(session)
    request = AIRequest(
        generation_type=GenerationType.SCRIPT,
        input_content={
            "topic_idea_id": topic_idea.id,
            "title": topic_idea.title,
            "core_view": topic_idea.core_view,
            "recommended_format": topic_idea.recommended_format,
            "video_type": topic_idea.video_type,
            "source_hotspot_ids": topic_idea.source_hotspot_ids or [],
            "source_hotspot_titles": topic_idea.source_hotspot_titles or [],
            "why_watch": topic_idea.why_watch,
            "why_save": topic_idea.why_save,
            "risk_note": topic_idea.risk_note,
            "comment_trigger": topic_idea.comment_trigger,
            "hook": topic_idea.hook,
            "account_style": account_style,
            "copywriting_samples": script_engine,
            "script_engine": script_engine,
            "style_validation": account_style["validation_rules"],
        },
        system_prompt=(
            "生成适合竖屏短视频的结构化脚本。必须读取账号风格库，保持大白话、"
            "高信息密度、克制表达，不做投资建议，不制造焦虑。"
        ),
        user_prompt="输出口播、分镜、字幕、标题、封面和剪辑清单。",
    )
    ai_result = generate_with_cache(session, request, provider=provider)
    payload = ai_result.output_content.get("script_asset")
    if not isinstance(payload, dict):
        raise ValueError("AI 输出缺少 script_asset。")

    # The immutable AI response may be reused through ``AiGeneration``, but a
    # ScriptAsset is a project's mutable working copy.  Sharing that row across
    # projects lets an edit in one project silently rewrite every other project
    # created from the same topic.
    script_asset = ScriptAsset(
        topic_idea_id=topic_idea.id,
        voiceover=_text(payload, "voiceover"),
        storyboard=_list(payload, "storyboard"),
        subtitles=_list(payload, "subtitles"),
        animation_prompts=_list(payload, "animation_prompts"),
        title_options=_list(payload, "title_options"),
        cover_copy=_list(payload, "cover_copy"),
        publish_copy=_dict(payload, "publish_copy"),
        edit_checklist=_list(payload, "edit_checklist"),
    )
    session.add(script_asset)
    topic_idea.hot_item.has_script = True
    session.add(topic_idea.hot_item)
    session.commit()
    session.refresh(script_asset)
    return ScriptGenerationResult(script_asset, ai_result.from_cache, ai_result.input_hash)


def _text(payload: dict[str, Any], key: str) -> str:
    return str(payload.get(key, "") or "").strip()


def _list(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = payload.get(key, [])
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _dict(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key, {})
    return value if isinstance(value, dict) else {}
