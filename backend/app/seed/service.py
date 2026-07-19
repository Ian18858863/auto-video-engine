from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.models import CopywritingSample, HotItem, ProductionPipeline, TopicIdea, VideoPost
from app.pipeline.service import build_project_from_hotspot
from app.publishing.service import add_video_snapshot, create_video_post


DEMO_PREFIX = "示例："


def seed_demo_data(
    session: Session,
    *,
    material_root: Path,
    project_root: Path | None = None,
) -> dict[str, Any]:
    if session.query(HotItem).filter(HotItem.title.like(f"{DEMO_PREFIX}%")).first():
        return {"created": False, "message": "示例数据已存在。"}

    now = datetime.now(timezone.utc)
    hotspots = [
        HotItem(
            source_platform="示例数据",
            source_type="manual",
            title=f"{DEMO_PREFIX}AI 搜索正在改变普通人获取信息的方式",
            url="https://example.com/demo-ai-search",
            published_at=now - timedelta(hours=2),
            summary="用于演示热点池、选题生成和项目流程。",
            tags=["AI", "搜索", "趋势"],
            category="AI",
            data_origin="sample",
            origin_note="本地演示数据",
            hot_score=82,
            topic_score=92,
            save_score=88,
            risk_score=0,
            freshness_score=20,
        ),
        HotItem(
            source_platform="示例数据",
            source_type="manual",
            title=f"{DEMO_PREFIX}企业开始把 AI 工具放进真实预算",
            url="https://example.com/demo-ai-budget",
            published_at=now - timedelta(hours=5),
            summary="用于演示商业逻辑拆解选题。",
            tags=["AI", "商业"],
            category="商业",
            data_origin="sample",
            origin_note="本地演示数据",
            hot_score=78,
            topic_score=90,
            save_score=85,
            risk_score=0,
            freshness_score=16,
        ),
        HotItem(
            source_platform="示例数据",
            source_type="manual",
            title=f"{DEMO_PREFIX}消费平台开始重做低价会员",
            url="https://example.com/demo-consumer-member",
            published_at=now - timedelta(hours=8),
            summary="用于演示消费趋势和收藏清单内容。",
            tags=["消费", "会员", "商业逻辑"],
            category="消费",
            data_origin="sample",
            origin_note="本地演示数据",
            hot_score=75,
            topic_score=84,
            save_score=91,
            risk_score=0,
            freshness_score=16,
        ),
    ]
    session.add_all(hotspots)
    session.commit()
    for item in hotspots:
        session.refresh(item)

    ideas = [
        TopicIdea(
            hot_item_id=hotspots[0].id,
            title=f"{DEMO_PREFIX}AI 搜索背后真正变的是信息入口",
            core_view="普通人不用追每个工具，先看谁掌握新的信息入口。",
            recommended_format="图文/PPT 动画为主体，右下角数字人辅助讲解",
            why_watch="能快速听懂 AI 搜索为什么重要。",
            why_save="可以复用来判断后续 AI 工具。",
            comment_trigger="你现在更常用搜索还是问 AI？",
            risk_note="不做投资建议。",
            recommended_duration="45-60 秒",
            hook="这件事表面是工具变化，背后是信息入口变化。",
            video_type="single_deep_dive",
            generation_source="sample",
            recommendation_score=88,
            recommendation_bucket="today",
            source_hotspot_ids=[hotspots[0].id],
            source_hotspot_titles=[hotspots[0].title],
            is_example=True,
        ),
        TopicIdea(
            hot_item_id=hotspots[1].id,
            title=f"{DEMO_PREFIX}企业买 AI 工具，普通人该看什么",
            core_view="真正重要的不是热度，而是企业愿不愿意付钱。",
            recommended_format="商业逻辑拆解",
            why_watch="解释 AI 工具从尝鲜到预算的变化。",
            why_save="能作为判断工具价值的清单。",
            comment_trigger="你觉得 AI 工具现在值不值得付费？",
            risk_note="不推荐具体公司或股票。",
            recommended_duration="50 秒",
            hook="别只看谁又发了新模型，先看企业愿不愿意掏钱。",
            video_type="single_deep_dive",
            generation_source="sample",
            recommendation_score=86,
            recommendation_bucket="today",
            source_hotspot_ids=[hotspots[1].id],
            source_hotspot_titles=[hotspots[1].title],
            is_example=True,
        ),
        TopicIdea(
            hot_item_id=hotspots[2].id,
            title=f"{DEMO_PREFIX}低价会员为什么又被平台重视",
            core_view="钱更谨慎时，平台要用低门槛留住用户。",
            recommended_format="收藏清单型",
            why_watch="用大白话解释消费平台变化。",
            why_save="可以复用到其他会员和订阅产品。",
            comment_trigger="你会为了低价会员留下来吗？",
            risk_note="避免绝对预测。",
            recommended_duration="45 秒",
            hook="这个变化不一定马上影响你，但它说明平台开始更重视确定性收入。",
            video_type="single_deep_dive",
            generation_source="sample",
            recommendation_score=82,
            recommendation_bucket="backup",
            source_hotspot_ids=[hotspots[2].id],
            source_hotspot_titles=[hotspots[2].title],
            is_example=True,
        ),
    ]
    session.add_all(ideas)
    session.add(
        CopywritingSample(
            title=f"{DEMO_PREFIX}小白转译开头",
            sample_type="开头钩子",
            sample_text="这条新闻普通人不用全看，关键是看懂背后的变化。",
            scene="小白转译",
            content_direction="AI 工具",
            structure_type="小白转译型",
            why_reference="短、直接，降低理解门槛。",
            reusable_points="先告诉用户不用看全部细节，再给核心逻辑。",
            avoid_points="不要直接复制原句。",
            tags=["示例数据", "小白转译"],
            enabled=True,
            is_key_reference=True,
            notes="示例数据，可清空。",
        )
    )
    session.commit()

    project = build_project_from_hotspot(
        session,
        hotspots[0].id,
        project_name=f"{DEMO_PREFIX}AI 搜索内容项目",
        selected_topic_id=ideas[0].id,
        material_root=material_root,
        project_root=project_root,
    )
    post = create_video_post(
        session,
        {
            "pipeline_id": project.pipeline_id,
            "platform": "抖音",
            "video_title": f"{DEMO_PREFIX}AI 搜索背后的信息入口",
            "published_at": now - timedelta(hours=26),
            "video_url": "https://example.com/demo-video",
            "topic_type": "AI",
            "video_format": "图文/PPT 动画 + 右下角数字人辅助",
            "hook_type": "小白转译",
            "duration_seconds": 52,
            "views": 1000,
            "likes": 80,
            "comments": 16,
            "favorites": 42,
            "shares": 18,
            "followers_gained": 7,
            "completion_rate": 45,
            "average_watch_seconds": 31,
            "notes": "示例数据，可清空。",
        },
        captured_at=now - timedelta(hours=2),
    )
    add_video_snapshot(
        session,
        post.id,
        {
            "views": 1600,
            "likes": 132,
            "comments": 25,
            "favorites": 76,
            "shares": 35,
            "followers_gained": 15,
            "completion_rate": 49,
            "average_watch_seconds": 34,
            "notes": "示例数据：24 小时后仍增长。",
        },
        captured_at=now,
    )
    return {
        "created": True,
        "message": "示例数据已创建。",
        "hotspot_count": 3,
        "topic_idea_count": 3,
        "pipeline_id": project.pipeline_id,
        "video_post_id": post.id,
    }


def clear_demo_data(session: Session) -> dict[str, Any]:
    videos = (
        session.query(VideoPost)
        .filter(VideoPost.video_title.like(f"{DEMO_PREFIX}%"))
        .all()
    )
    pipelines = (
        session.query(ProductionPipeline)
        .filter(ProductionPipeline.project_name.like(f"{DEMO_PREFIX}%"))
        .all()
    )
    samples = (
        session.query(CopywritingSample)
        .filter(CopywritingSample.title.like(f"{DEMO_PREFIX}%"))
        .all()
    )
    topics = (
        session.query(TopicIdea)
        .filter(TopicIdea.title.like(f"{DEMO_PREFIX}%"))
        .all()
    )
    hotspots = (
        session.query(HotItem)
        .filter(HotItem.title.like(f"{DEMO_PREFIX}%"))
        .all()
    )
    counts = {
        "video_post_count": len(videos),
        "pipeline_count": len(pipelines),
        "sample_count": len(samples),
        "topic_idea_count": len(topics),
        "hotspot_count": len(hotspots),
    }
    for collection in (videos, pipelines, samples, topics, hotspots):
        for item in collection:
            session.delete(item)
    session.commit()
    return {
        "deleted": True,
        **counts,
    }
