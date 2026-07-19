from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.hotspots.archive import archive_expired_hotspots
from app.main import app
from app.models import AiGeneration, HotItem, ProductionPipeline, ScriptAsset, TopicIdea
from app.topic_ideas.pool_service import (
    get_topic_idea,
    list_topic_ideas,
    select_topic_idea,
)
from app.topic_ideas.event_scoring import event_score, score_breakdown_dict
from app.topic_ideas.local_generator import (
    compose_collection_from_events,
    generate_local_daily_topic_candidates,
    update_topic_recommendation_bucket,
)


def make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return Session(engine)


def add_hotspot(
    session: Session,
    suffix: str,
    *,
    title: str | None = None,
    url: str | None = None,
    summary: str = "用于候选选题池测试。",
    data_origin: str = "real",
    published_at: datetime | None = None,
    source_platform: str = "测试公开源",
    category: str = "AI",
) -> HotItem:
    item = HotItem(
        source_platform=source_platform,
        source_type="news",
        title=title or f"热点 {suffix}",
        url=f"https://example.com/topic-pool-{suffix}" if url is None else url,
        published_at=published_at or datetime.now(timezone.utc),
        summary=summary,
        tags=["AI", "商业"],
        category=category,
        hot_score=88,
        topic_score=92,
        save_score=86,
        risk_score=0,
        freshness_score=20,
        data_origin=data_origin,
    )
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


def add_topic_idea(
    session: Session,
    hotspot: HotItem,
    suffix: str,
    *,
    is_selected: bool = False,
) -> TopicIdea:
    idea = TopicIdea(
        hot_item_id=hotspot.id,
        title=f"选题 {suffix}",
        core_view=f"核心观点 {suffix}",
        recommended_format="数字人口播",
        why_watch="能快速看懂变化。",
        why_save="判断框架可以复用。",
        comment_trigger="你会跟进还是观望？",
        risk_note="避免投资建议。",
        recommended_duration="45 秒",
        hook="为什么这个变化现在值得关注？",
        is_selected=is_selected,
    )
    session.add(idea)
    session.commit()
    session.refresh(idea)
    return idea


def _frontend_recommendation_window(items, *, limit: int = 15, source_limit: int = 3):
    source_counts: dict[str, int] = {}
    selected = []
    for item in sorted(items, key=lambda row: row.recommendation_score, reverse=True):
        source = item.source_platforms[0] if item.source_platforms else "公开来源"
        if source_counts.get(source, 0) >= source_limit:
            continue
        selected.append(item)
        source_counts[source] = source_counts.get(source, 0) + 1
        if len(selected) >= limit:
            break
    return selected


def test_list_topic_ideas_supports_hotspot_filter_and_script_status():
    with make_session() as session:
        first_hotspot = add_hotspot(session, "A")
        second_hotspot = add_hotspot(session, "B")
        first_idea = add_topic_idea(session, first_hotspot, "A1")
        add_topic_idea(session, first_hotspot, "A2")
        add_topic_idea(session, second_hotspot, "B1")
        session.add(ScriptAsset(topic_idea_id=first_idea.id, voiceover="测试口播"))
        session.commit()
        generation_count = session.query(AiGeneration).count()

        result = list_topic_ideas(session, hotspot_id=first_hotspot.id)

        assert result.total == 2
        assert {item.hotspot_title for item in result.items} == {"热点 A"}
        scripted_item = next(item for item in result.items if item.title == "选题 A1")
        assert scripted_item.has_script is True
        assert scripted_item.script_asset_id is not None
        assert scripted_item.source_credibility == "高"
        assert scripted_item.source_hotspot_urls == [first_hotspot.url]
        assert {item.title for item in result.hotspots} == {"热点 A", "热点 B"}
        assert session.query(AiGeneration).count() == generation_count


def test_get_and_select_topic_idea_are_clear_and_exclusive():
    with make_session() as session:
        hotspot = add_hotspot(session, "选择")
        first = add_topic_idea(session, hotspot, "1", is_selected=True)
        second = add_topic_idea(session, hotspot, "2")

        selected = select_topic_idea(session, second.id)

        assert selected is not None
        assert selected.is_selected is True
        assert session.get(TopicIdea, first.id).is_selected is False
        assert get_topic_idea(session, second.id).title == "选题 2"
        assert get_topic_idea(session, 999) is None


def test_local_daily_generation_creates_today_event_recommendations_without_collection():
    with make_session() as session:
        for index in range(6):
            item = add_hotspot(
                session,
                f"自动{index}",
                source_platform=f"测试公开源{index}",
            )
            item.hot_score = 90 - index
            item.topic_score = 96 - index
            item.save_score = 85 - index
            session.add(item)
        session.commit()
        generation_count = session.query(AiGeneration).count()

        generated = generate_local_daily_topic_candidates(
            session,
            max_hotspots=6,
            today_limit=5,
        )
        result = list_topic_ideas(session)
        today_items = [
            item for item in result.items if item.recommendation_bucket == "today"
        ]
        backup_items = [
            item for item in result.items if item.recommendation_bucket == "backup"
        ]

        assert generated
        assert len(today_items) == 5
        assert backup_items
        assert all(
            item.video_type in {"collection_event", "single_deep_dive_candidate"}
            for item in today_items
        )
        assert session.query(AiGeneration).count() == generation_count


def test_local_daily_generation_keeps_original_hotspot_title_as_candidate_title():
    original_title = "国内首例：垣信卫星实现无改造手机直连卫星通话，通话质量比肩 5G"
    with make_session() as session:
        add_hotspot(
            session,
            "原文标题",
            title=original_title,
            summary="卫星通信技术进入更接近普通手机使用的阶段。",
        )

        generate_local_daily_topic_candidates(session, max_hotspots=1, today_limit=1)
        stored = session.query(TopicIdea).one()
        listed = list_topic_ideas(session).items[0]

        assert stored.title == original_title
        assert listed.title == original_title
        assert listed.source_hotspot_titles == [original_title]
        assert "别只看热闹" not in listed.title


def test_event_score_uses_current_five_dimension_rule():
    with make_session() as session:
        hotspot = add_hotspot(
            session,
            "五维评分",
            title="AI 搜索正在改变普通人获取信息的方式",
            summary="信息获取方式变化，影响平台流量、广告模式、内容生态和普通人学习工作。",
            category="AI",
        )

        score = event_score(hotspot, source_count=3)
        breakdown = score_breakdown_dict(score)

        assert set(breakdown) == {
            "impact",
            "account_fit",
            "freshness_heat",
            "cross_source_resonance",
            "ordinary_relevance",
            "risk",
            "base_total",
            "bonus_total",
            "total",
        }
        assert breakdown["ordinary_relevance"] >= 70
        assert breakdown["cross_source_resonance"] >= 75
        assert "save_value" not in breakdown
        assert "explanation_value" not in breakdown


def test_local_daily_generation_merges_same_event_across_sources():
    title = "AI 搜索正在改变普通人获取信息的方式"
    with make_session() as session:
        add_hotspot(
            session,
            "跨源A",
            title=title,
            source_platform="36氪",
            summary="AI 搜索影响平台流量和广告模式。",
            category="AI",
        )
        add_hotspot(
            session,
            "跨源B",
            title=title,
            source_platform="IT之家",
            summary="AI 搜索影响普通人获取信息的方式。",
            category="AI",
        )

        generate_local_daily_topic_candidates(session, max_hotspots=5, today_limit=5)
        result = list_topic_ideas(session)

        assert result.total == 1
        item = result.items[0]
        assert item.title == title
        assert item.source_platforms == ["36氪", "IT之家"]
        assert len(item.source_hotspot_ids) == 2
        assert item.score_breakdown["cross_source_resonance"] >= 75


def test_local_daily_generation_limits_same_source_in_today_recommendations():
    with make_session() as session:
        for index in range(6):
            item = add_hotspot(
                session,
                f"36kr{index}",
                source_platform="36氪",
                category="科技",
            )
            item.hot_score = 95 - index
            item.topic_score = 98 - index
            item.save_score = 90 - index
            session.add(item)
        for index in range(3):
            item = add_hotspot(
                session,
                f"baidu{index}",
                source_platform="百度热搜",
                category="社会",
            )
            item.hot_score = 80 - index
            item.topic_score = 84 - index
            item.save_score = 78 - index
            session.add(item)
        session.commit()

        generate_local_daily_topic_candidates(session, max_hotspots=9, today_limit=5)
        result = list_topic_ideas(session)
        today_items = [
            item for item in result.items if item.recommendation_bucket == "today"
        ]
        source_counts: dict[str, int] = {}
        for item in today_items:
            hotspot = session.get(HotItem, item.hot_item_id)
            assert hotspot is not None
            source_counts[hotspot.source_platform] = (
                source_counts.get(hotspot.source_platform, 0) + 1
            )

        assert len(today_items) == 5
        assert source_counts["36氪"] <= 3
        assert source_counts.get("百度热搜", 0) >= 1


def test_local_daily_generation_does_not_fill_today_with_one_source_only():
    with make_session() as session:
        for index in range(8):
            item = add_hotspot(
                session,
                f"单源{index}",
                source_platform="36氪",
                category="科技",
            )
            item.hot_score = 99 - index
            item.topic_score = 99 - index
            item.save_score = 92 - index
            session.add(item)
        session.commit()

        generate_local_daily_topic_candidates(session, max_hotspots=8, today_limit=5)
        result = list_topic_ideas(session)
        today_items = [
            item for item in result.items if item.recommendation_bucket == "today"
        ]

        assert len(today_items) == 3
        assert all(item.source_platforms == ["36氪"] for item in today_items)


def test_local_daily_generation_prioritizes_source_and_category_diversity_for_today():
    with make_session() as session:
        for index in range(5):
            item = add_hotspot(
                session,
                f"多36kr{index}",
                source_platform="36氪",
                category="科技",
            )
            item.hot_score = 98 - index
            item.topic_score = 99 - index
            item.save_score = 90 - index
            session.add(item)
        for index in range(4):
            item = add_hotspot(
                session,
                f"多it{index}",
                source_platform="IT之家",
                category="科技",
            )
            item.hot_score = 96 - index
            item.topic_score = 97 - index
            item.save_score = 88 - index
            session.add(item)
        diverse_sources = [
            ("微博热搜网页版", "社会", 75),
            ("百度热搜网页版", "财经", 74),
            ("小红书热点网页版", "消费", 73),
            ("B站热门网页版", "AI", 72),
        ]
        for index, (source, category, score) in enumerate(diverse_sources):
            item = add_hotspot(
                session,
                f"多样{index}",
                source_platform=source,
                category=category,
            )
            item.hot_score = score
            item.topic_score = score
            item.save_score = score
            session.add(item)
        session.commit()

        generate_local_daily_topic_candidates(session, max_hotspots=12, today_limit=5)
        result = list_topic_ideas(session)
        today_items = [
            item for item in result.items if item.recommendation_bucket == "today"
        ]
        source_counts: dict[str, int] = {}
        category_counts: dict[str, int] = {}
        for item in today_items:
            hotspot = session.get(HotItem, item.hot_item_id)
            assert hotspot is not None
            source_counts[hotspot.source_platform] = (
                source_counts.get(hotspot.source_platform, 0) + 1
            )
            category_counts[hotspot.category] = category_counts.get(hotspot.category, 0) + 1

        assert len(today_items) == 5
        assert source_counts["36氪"] <= 3
        assert source_counts["IT之家"] <= 3
        assert max(source_counts.values()) <= 3


def test_topic_pool_rebalances_existing_today_recommendations_on_read():
    with make_session() as session:
        for index in range(6):
            hotspot = add_hotspot(
                session,
                f"旧36kr{index}",
                source_platform="36氪",
                category="科技",
            )
            hotspot.hot_score = 95 - index
            hotspot.topic_score = 98 - index
            hotspot.save_score = 90 - index
            idea = add_topic_idea(session, hotspot, f"旧36kr{index}")
            idea.generation_source = "local_rule"
            idea.recommendation_bucket = "today"
            idea.recommendation_score = 100 - index
            idea.source_hotspot_ids = [hotspot.id]
            idea.source_hotspot_titles = [hotspot.title]
            session.add_all([hotspot, idea])
        for index in range(3):
            hotspot = add_hotspot(
                session,
                f"旧百度{index}",
                source_platform="百度热搜",
                category="社会",
            )
            hotspot.hot_score = 80 - index
            hotspot.topic_score = 84 - index
            hotspot.save_score = 78 - index
            idea = add_topic_idea(session, hotspot, f"旧百度{index}")
            idea.generation_source = "local_rule"
            idea.recommendation_bucket = "today"
            idea.recommendation_score = 80 - index
            idea.source_hotspot_ids = [hotspot.id]
            idea.source_hotspot_titles = [hotspot.title]
            session.add_all([hotspot, idea])
        session.commit()

        result = list_topic_ideas(session)
        today_items = [
            item for item in result.items if item.recommendation_bucket == "today"
        ]
        source_counts: dict[str, int] = {}
        for item in today_items:
            hotspot = session.get(HotItem, item.hot_item_id)
            assert hotspot is not None
            source_counts[hotspot.source_platform] = (
                source_counts.get(hotspot.source_platform, 0) + 1
            )

        assert len(today_items) == 5
        assert source_counts["36氪"] <= 3
        assert source_counts.get("百度热搜", 0) >= 1


def test_topic_pool_returns_enough_diverse_candidates_for_frontend_recommendation_window():
    with make_session() as session:
        for index in range(10):
            hotspot = add_hotspot(
                session,
                f"窗口IT{index}",
                title=f"IT之家高分科技事件 {index}",
                source_platform="IT之家",
                category="科技",
                summary="科技产业变化会影响普通人选择工具和设备。",
            )
            idea = add_topic_idea(session, hotspot, f"窗口IT{index}")
            idea.title = hotspot.title
            idea.generation_source = "local_rule"
            idea.video_type = "collection_event"
            idea.recommendation_score = 70 - index
            idea.source_hotspot_ids = [hotspot.id]
            idea.source_hotspot_titles = [hotspot.title]
            session.add(idea)

        diverse_sources = [
            "微博热搜网页版",
            "百度热搜网页版",
            "抖音热点网页版",
            "小红书公开发现页",
            "B站科技区排行网页版",
            "财联社电报网页版",
            "Google News",
            "爱范儿",
            "36氪",
            "Hacker News",
            "Reddit",
            "澎湃新闻",
        ]
        categories = ["社会", "财经", "科技", "消费", "AI", "国际", "生活", "财经"]
        for index, source in enumerate(diverse_sources):
            hotspot = add_hotspot(
                session,
                f"窗口多源{index}",
                title=f"{source} 的平台规则和消费趋势变化 {index}",
                source_platform=source,
                category=categories[index % len(categories)],
                summary="这类事件有明确变化、普通人相关度和可讲清楚的背景。",
            )
            idea = add_topic_idea(session, hotspot, f"窗口多源{index}")
            idea.title = hotspot.title
            idea.generation_source = "local_rule"
            idea.video_type = "collection_event"
            idea.recommendation_score = 49 - index * 0.5
            idea.source_hotspot_ids = [hotspot.id]
            idea.source_hotspot_titles = [hotspot.title]
            session.add(idea)
        session.commit()

        result = list_topic_ideas(session, page_size=100)
        recommendation_window = _frontend_recommendation_window(result.items)

        assert len(recommendation_window) == 15
        source_counts: dict[str, int] = {}
        for item in recommendation_window:
            source = item.source_platforms[0]
        source_counts[source] = source_counts.get(source, 0) + 1
    assert max(source_counts.values()) <= 3
    assert len({item.source_platforms[0] for item in recommendation_window}) >= 8


def test_local_daily_generation_keeps_candidates_before_source_cap_ranking():
    with make_session() as session:
        for index in range(6):
            item = add_hotspot(
                session,
                f"生成36kr{index}",
                source_platform="36氪",
                category="科技",
            )
            item.hot_score = 95 - index
            item.topic_score = 98 - index
            item.save_score = 90 - index
            session.add(item)
        for index in range(3):
            item = add_hotspot(
                session,
                f"生成百度{index}",
                source_platform="百度热搜",
                category="社会",
            )
            item.hot_score = 80 - index
            item.topic_score = 84 - index
            item.save_score = 78 - index
            session.add(item)
        session.commit()

        generate_local_daily_topic_candidates(session, max_hotspots=9, today_limit=5)
        ideas = session.query(TopicIdea).all()
        source_counts: dict[str, int] = {}
        for idea in ideas:
            hotspot = session.get(HotItem, idea.hot_item_id)
            assert hotspot is not None
            source_counts[hotspot.source_platform] = (
                source_counts.get(hotspot.source_platform, 0) + 1
            )

        assert len(ideas) == 9
        assert source_counts["36氪"] == 6
        assert source_counts["百度热搜"] == 3


def test_local_daily_generation_considers_browser_sources_outside_top_score_slice():
    with make_session() as session:
        for index in range(220):
            item = add_hotspot(
                session,
                f"高分IT{index}",
                source_platform="IT之家",
                category="科技",
            )
            item.hot_score = 95
            item.topic_score = 95
            item.save_score = 88
            session.add(item)
        browser_item = add_hotspot(
            session,
            "浏览器微博",
            title="微博热搜上的消费补贴规则变化",
            source_platform="微博热搜网页版",
            category="社会",
            summary="消费补贴和平台规则变化，普通人会直接感知。",
        )
        browser_item.hot_score = 45
        browser_item.topic_score = 35
        browser_item.save_score = 30
        session.add(browser_item)
        session.commit()

        generate_local_daily_topic_candidates(session, max_hotspots=15, today_limit=5)
        ideas = session.query(TopicIdea).all()
        source_hotspot_ids = {
            source_id
            for idea in ideas
            for source_id in (idea.source_hotspot_ids or [idea.hot_item_id])
        }

        assert browser_item.id in source_hotspot_ids


def test_topic_pool_excludes_manual_v1_acceptance_from_general_recommendations():
    with make_session() as session:
        official = add_hotspot(
            session,
            "正式热点",
            source_platform="微博热搜网页版",
            category="社会",
        )
        test_hotspot = add_hotspot(
            session,
            "跑通测试",
            title="AI 搜索正在改变普通人获取信息的方式",
            source_platform="手动跑通测试",
            category="AI",
            data_origin="manual",
        )
        official_idea = add_topic_idea(session, official, "正式")
        official_idea.generation_source = "local_rule"
        official_idea.recommendation_bucket = "today"
        official_idea.recommendation_score = 70
        official_idea.source_hotspot_ids = [official.id]
        official_idea.source_hotspot_titles = [official.title]
        test_idea = add_topic_idea(session, test_hotspot, "跑通")
        test_idea.generation_source = "local_rule"
        test_idea.recommendation_bucket = "today"
        test_idea.recommendation_score = 99
        test_idea.source_hotspot_ids = [test_hotspot.id]
        test_idea.source_hotspot_titles = [test_hotspot.title]
        session.add_all([official_idea, test_idea])
        session.commit()

        result = list_topic_ideas(session)

        assert {item.id for item in result.items} == {official_idea.id}
        assert all("手动跑通测试" not in item.source_platforms for item in result.items)


def test_topic_pool_excludes_mojibake_hotspots_from_general_recommendations():
    with make_session() as session:
        official = add_hotspot(
            session,
            "正式热点",
            source_platform="微博热搜网页版",
            category="社会",
        )
        dirty_hotspot = add_hotspot(
            session,
            "乱码热点",
            title="AI ????????????????",
            source_platform="????",
            category="AI",
            url="https://manual.local/trend-video-factory/dirty",
        )
        official_idea = add_topic_idea(session, official, "正式")
        official_idea.generation_source = "local_rule"
        official_idea.recommendation_bucket = "today"
        official_idea.recommendation_score = 70
        official_idea.source_hotspot_ids = [official.id]
        official_idea.source_hotspot_titles = [official.title]
        dirty_idea = add_topic_idea(session, dirty_hotspot, "乱码")
        dirty_idea.generation_source = "local_rule"
        dirty_idea.recommendation_bucket = "today"
        dirty_idea.recommendation_score = 99
        dirty_idea.source_hotspot_ids = [dirty_hotspot.id]
        dirty_idea.source_hotspot_titles = [dirty_hotspot.title]
        session.add_all([official_idea, dirty_idea])
        session.commit()

        result = list_topic_ideas(session)

        assert {item.id for item in result.items} == {official_idea.id}
        assert all("????" not in item.title for item in result.items)
        assert all("????" not in item.source_platforms for item in result.items)
        assert dirty_hotspot.title not in [hotspot.title for hotspot in result.hotspots]


def test_topic_pool_excludes_candidates_with_mojibake_source_hotspot_ids():
    with make_session() as session:
        official = add_hotspot(
            session,
            "正式热点",
            source_platform="微博热搜网页版",
            category="社会",
        )
        dirty_source = add_hotspot(
            session,
            "乱码来源",
            title="AI ????????????????",
            source_platform="????",
            category="AI",
            url="https://manual.local/trend-video-factory/dirty-source",
        )
        clean_idea = add_topic_idea(session, official, "正式")
        clean_idea.generation_source = "local_rule"
        clean_idea.recommendation_bucket = "today"
        clean_idea.recommendation_score = 70
        clean_idea.source_hotspot_ids = [official.id]
        clean_idea.source_hotspot_titles = [official.title]
        dirty_linked_idea = add_topic_idea(session, official, "引用乱码来源")
        dirty_linked_idea.generation_source = "local_rule"
        dirty_linked_idea.recommendation_bucket = "today"
        dirty_linked_idea.recommendation_score = 99
        dirty_linked_idea.source_hotspot_ids = [official.id, dirty_source.id]
        dirty_linked_idea.source_hotspot_titles = [official.title, dirty_source.title]
        session.add_all([clean_idea, dirty_linked_idea])
        session.commit()

        result = list_topic_ideas(session)

        assert {item.id for item in result.items} == {clean_idea.id}
        assert all(dirty_source.id not in item.source_hotspot_ids for item in result.items)


def test_topic_pool_excludes_low_value_navigation_profile_and_document_pages():
    with make_session() as session:
        useful = add_hotspot(
            session,
            "有效热点",
            title="AI 搜索正在改变普通人获取信息的方式",
            source_platform="微博热搜网页版",
            category="AI",
            summary="AI 搜索影响平台流量、广告模式和普通人获取信息方式。",
        )
        invalid_hotspots = [
            add_hotspot(
                session,
                "反馈页",
                title="用户反馈",
                url="https://www.cls.cn/our?nav=feedBack",
                source_platform="财联社电报网页版",
                category="财经",
            ),
            add_hotspot(
                session,
                "B站主页",
                title="影视飓风",
                url="https://space.bilibili.com/946974",
                source_platform="B站科技区排行网页版",
                category="科技",
            ),
            add_hotspot(
                session,
                "协议页",
                title="用户协议",
                url="https://www.bilibili.com/protocal/licence.html",
                source_platform="B站热门网页版",
                category="互联网趋势",
            ),
            add_hotspot(
                session,
                "消息页",
                title="回复我的",
                url="https://message.bilibili.com/",
                source_platform="B站热门网页版",
                category="互联网趋势",
            ),
            add_hotspot(
                session,
                "PDF备案",
                title="医疗器械网络交易服务第三方平台备案：(沪)网械平台备字[2019]第00006号",
                url="https://fe-video-qc.xhscdn.com/fe-platform/410dce57bc12a6d7e5808060e47644fbe46f68ff.pdf",
                source_platform="小红书公开发现页",
                category="生活",
            ),
        ]
        useful_idea = add_topic_idea(session, useful, "有效")
        useful_idea.generation_source = "local_rule"
        useful_idea.recommendation_bucket = "today"
        useful_idea.recommendation_score = 70
        useful_idea.source_hotspot_ids = [useful.id]
        useful_idea.source_hotspot_titles = [useful.title]
        session.add(useful_idea)
        for index, hotspot in enumerate(invalid_hotspots):
            idea = add_topic_idea(session, hotspot, f"垃圾{index}")
            idea.generation_source = "local_rule"
            idea.recommendation_bucket = "today"
            idea.recommendation_score = 99 - index
            idea.source_hotspot_ids = [hotspot.id]
            idea.source_hotspot_titles = [hotspot.title]
            session.add(idea)
        session.commit()

        result = list_topic_ideas(session)

        assert {item.id for item in result.items} == {useful_idea.id}
        listed_titles = {item.hotspot_title for item in result.items}
        assert "用户反馈" not in listed_titles
        assert "影视飓风" not in listed_titles
        assert "用户协议" not in listed_titles
        assert "回复我的" not in listed_titles


def test_topic_pool_excludes_stock_ticks_short_labels_and_low_value_lifestyle():
    with make_session() as session:
        useful = add_hotspot(
            session,
            "有效趋势",
            title="AI 搜索正在改变普通人获取信息的方式",
            source_platform="微博热搜网页版",
            category="AI",
            summary="AI 搜索影响平台流量、广告模式和普通人获取信息方式。",
        )
        invalid_hotspots = [
            add_hotspot(
                session,
                "股票涨跌",
                title="通鼎互联-7.53%",
                source_platform="财联社电报网页版",
                category="财经",
                summary="单只股票涨跌幅。",
            ),
            add_hotspot(
                session,
                "短产品名",
                title="点点 ai",
                source_platform="小红书公开发现页",
                category="AI",
                summary="只有一个产品名，缺少事件信息。",
            ),
            add_hotspot(
                session,
                "低价值生活",
                title="01:17 1.1万 新婚小夫妻备孕不用愁！有神仙AI备孕顾问",
                source_platform="抖音热点网页版",
                category="生活",
                summary="家庭日常和备孕营销内容。",
            ),
        ]
        useful_idea = add_topic_idea(session, useful, "有效趋势")
        useful_idea.generation_source = "local_rule"
        useful_idea.recommendation_bucket = "today"
        useful_idea.recommendation_score = 70
        useful_idea.source_hotspot_ids = [useful.id]
        useful_idea.source_hotspot_titles = [useful.title]
        session.add(useful_idea)
        for index, hotspot in enumerate(invalid_hotspots):
            idea = add_topic_idea(session, hotspot, f"低价值{index}")
            idea.generation_source = "local_rule"
            idea.recommendation_bucket = "today"
            idea.recommendation_score = 99 - index
            idea.source_hotspot_ids = [hotspot.id]
            idea.source_hotspot_titles = [hotspot.title]
            session.add(idea)
        session.commit()

        result = list_topic_ideas(session)

        listed_titles = {item.hotspot_title for item in result.items}
        assert useful.title in listed_titles
        assert "通鼎互联-7.53%" not in listed_titles
        assert "点点 ai" not in listed_titles
        assert "01:17 1.1万 新婚小夫妻备孕不用愁！有神仙AI备孕顾问" not in listed_titles


def test_topic_pool_default_only_shows_local_event_candidates_not_ai_angles_or_collections():
    with make_session() as session:
        hotspot = add_hotspot(
            session,
            "正式事件",
            title="AI 搜索正在改变普通人获取信息的方式",
            source_platform="微博热搜网页版",
            category="AI",
            summary="AI 搜索影响平台流量、广告模式和普通人获取信息方式。",
        )
        local_event = add_topic_idea(session, hotspot, "正式事件")
        local_event.title = hotspot.title
        local_event.generation_source = "local_rule"
        local_event.video_type = "collection_event"
        local_event.recommendation_bucket = "today"
        local_event.recommendation_score = 70
        local_event.source_hotspot_ids = [hotspot.id]
        local_event.source_hotspot_titles = [hotspot.title]
        ai_angle = add_topic_idea(session, hotspot, "AI角度")
        ai_angle.title = f"别只看热闹，真正的问题是它靠什么成立：{hotspot.title}"
        ai_angle.generation_source = "fake_provider"
        ai_angle.recommendation_bucket = "backup"
        ai_angle.recommendation_score = 99
        ai_angle.source_hotspot_ids = [hotspot.id]
        ai_angle.source_hotspot_titles = [hotspot.title]
        collection = add_topic_idea(session, hotspot, "合集")
        collection.title = "今日5件事"
        collection.generation_source = "local_rule"
        collection.video_type = "multi_hotspot_collection"
        collection.recommendation_bucket = "today"
        collection.recommendation_score = 100
        collection.source_hotspot_ids = [hotspot.id]
        collection.source_hotspot_titles = [hotspot.title]
        session.add_all([local_event, ai_angle, collection])
        session.commit()

        result = list_topic_ideas(session)
        hotspot_result = list_topic_ideas(session, hotspot_id=hotspot.id)

        assert [item.id for item in result.items] == [local_event.id]
        assert result.items[0].title == hotspot.title
        assert ai_angle.id in {item.id for item in hotspot_result.items}


def test_topic_pool_excludes_stale_local_events_even_if_hotspot_is_still_active():
    old_time = datetime.now(timezone.utc) - timedelta(hours=48)
    with make_session() as session:
        fresh = add_hotspot(
            session,
            "新鲜事件",
            title="AI 搜索正在改变普通人获取信息的方式",
            source_platform="微博热搜网页版",
            category="AI",
            summary="AI 搜索影响平台流量、广告模式和普通人获取信息方式。",
        )
        stale = add_hotspot(
            session,
            "过期但未归档",
            title="挪威将禁止小学生使用生成式人工智能",
            source_platform="36氪",
            category="财经",
            published_at=old_time,
            summary="超过 36 小时的旧热点不应继续进入今日推荐。",
        )
        stale.lifecycle_status = "active"
        fresh_idea = add_topic_idea(session, fresh, "新鲜事件")
        fresh_idea.title = fresh.title
        fresh_idea.generation_source = "local_rule"
        fresh_idea.video_type = "collection_event"
        fresh_idea.recommendation_bucket = "today"
        fresh_idea.recommendation_score = 70
        fresh_idea.source_hotspot_ids = [fresh.id]
        fresh_idea.source_hotspot_titles = [fresh.title]
        stale_idea = add_topic_idea(session, stale, "过期事件")
        stale_idea.title = stale.title
        stale_idea.generation_source = "local_rule"
        stale_idea.video_type = "collection_event"
        stale_idea.recommendation_bucket = "today"
        stale_idea.recommendation_score = 99
        stale_idea.is_selected = True
        stale_idea.source_hotspot_ids = [stale.id]
        stale_idea.source_hotspot_titles = [stale.title]
        session.add_all([stale, fresh_idea, stale_idea])
        session.commit()

        result = list_topic_ideas(session)

        listed_titles = {item.title for item in result.items}
        assert fresh.title in listed_titles
        assert stale.title not in listed_titles


def test_local_daily_generation_skips_low_value_navigation_profile_and_document_pages():
    with make_session() as session:
        useful = add_hotspot(
            session,
            "有效生成",
            title="AI 搜索正在改变普通人获取信息的方式",
            source_platform="微博热搜网页版",
            category="AI",
            summary="AI 搜索影响平台流量、广告模式和普通人获取信息方式。",
        )
        useful.hot_score = 60
        useful.topic_score = 65
        session.add(useful)
        for title, url, source in [
            ("用户反馈", "https://www.cls.cn/our?nav=feedBack", "财联社电报网页版"),
            ("影视飓风", "https://space.bilibili.com/946974", "B站科技区排行网页版"),
            ("用户协议", "https://www.bilibili.com/protocal/licence.html", "B站热门网页版"),
            ("回复我的", "https://message.bilibili.com/", "B站热门网页版"),
            (
                "医疗器械网络交易服务第三方平台备案：(沪)网械平台备字[2019]第00006号",
                "https://fe-video-qc.xhscdn.com/fe-platform/410dce57bc12a6d7e5808060e47644fbe46f68ff.pdf",
                "小红书公开发现页",
            ),
        ]:
            item = add_hotspot(
                session,
                title,
                title=title,
                url=url,
                source_platform=source,
                category="科技",
            )
            item.hot_score = 99
            item.topic_score = 99
            item.save_score = 99
            session.add(item)
        session.commit()

        generate_local_daily_topic_candidates(session, max_hotspots=10, today_limit=5)
        titles = {idea.title for idea in session.query(TopicIdea).all()}

        assert "AI 搜索正在改变普通人获取信息的方式" in titles
        assert "用户反馈" not in titles
        assert "影视飓风" not in titles
        assert "用户协议" not in titles
        assert "回复我的" not in titles


def test_local_daily_generation_skips_stock_ticks_short_labels_and_lifestyle_noise():
    with make_session() as session:
        useful = add_hotspot(
            session,
            "有效生成趋势",
            title="AI 搜索正在改变普通人获取信息的方式",
            source_platform="微博热搜网页版",
            category="AI",
            summary="AI 搜索影响平台流量、广告模式和普通人获取信息方式。",
        )
        useful.hot_score = 60
        useful.topic_score = 65
        session.add(useful)
        for title, source, category in [
            ("通鼎互联-7.53%", "财联社电报网页版", "财经"),
            ("今世缘+3.07%", "财联社电报网页版", "财经"),
            ("点点 ai", "小红书公开发现页", "AI"),
            ("01:17 1.1万 新婚小夫妻备孕不用愁！有神仙AI备孕顾问", "抖音热点网页版", "生活"),
        ]:
            item = add_hotspot(
                session,
                title,
                title=title,
                source_platform=source,
                category=category,
                summary="低价值候选测试。",
            )
            item.hot_score = 99
            item.topic_score = 99
            item.save_score = 99
            session.add(item)
        session.commit()

        generate_local_daily_topic_candidates(session, max_hotspots=10, today_limit=5)
        titles = {idea.title for idea in session.query(TopicIdea).all()}

        assert "AI 搜索正在改变普通人获取信息的方式" in titles
        assert "通鼎互联-7.53%" not in titles
        assert "今世缘+3.07%" not in titles
        assert "点点 ai" not in titles
        assert "01:17 1.1万 新婚小夫妻备孕不用愁！有神仙AI备孕顾问" not in titles


def test_local_daily_generation_skips_low_value_entertainment_candidates():
    with make_session() as session:
        movie = add_hotspot(
            session,
            "电影",
            title="某电影 6 月 26 日上映",
            summary="电影档期消息。",
        )
        movie.category = "娱乐"
        movie.tags = ["电影"]
        movie.hot_score = 98
        movie.topic_score = 98
        movie.save_score = 90
        ai = add_hotspot(
            session,
            "AI搜索",
            title="AI 搜索正在改变普通人获取信息的方式",
            summary="AI 搜索影响平台流量、广告模式和内容生态。",
        )
        session.add_all([movie, ai])
        session.commit()

        generate_local_daily_topic_candidates(session, max_hotspots=5, today_limit=5)
        result = list_topic_ideas(session)
        titles = {item.hotspot_title for item in result.items}

        assert "某电影 6 月 26 日上映" not in titles
        assert "AI 搜索正在改变普通人获取信息的方式" in titles


def test_compose_collection_from_selected_events_creates_daily_collection():
    with make_session() as session:
        hotspots = [
            add_hotspot(
                session,
                f"组合{index}",
                source_platform=f"测试公开源{index}",
            )
            for index in range(4)
        ]
        generate_local_daily_topic_candidates(session, max_hotspots=4, today_limit=4)
        events = (
            session.query(TopicIdea)
            .filter(TopicIdea.video_type == "collection_event")
            .order_by(TopicIdea.id)
            .all()
        )

        collection = compose_collection_from_events(
            session,
            [event.id for event in events[:4]],
        )
        result = get_topic_idea(session, collection.id)

        assert result is not None
        assert result.video_type == "multi_hotspot_collection"
        assert result.title == "今日4件事"
        assert result.source_hotspot_ids == [event.hot_item_id for event in events[:4]]
        assert result.is_selected is True


def test_compose_collection_allows_one_selected_event():
    with make_session() as session:
        add_hotspot(session, "单条")
        generate_local_daily_topic_candidates(session, max_hotspots=1, today_limit=1)
        event = session.query(TopicIdea).one()

        collection = compose_collection_from_events(session, [event.id])
        result = get_topic_idea(session, collection.id)

        assert result is not None
        assert result.title == "今日1件事"
        assert result.recommended_format == "单条深挖型"
        assert result.source_hotspot_ids == [event.hot_item_id]


def test_compose_collection_allows_two_selected_events():
    with make_session() as session:
        for index in range(2):
            add_hotspot(session, f"双事件{index}")
        generate_local_daily_topic_candidates(session, max_hotspots=2, today_limit=2)
        events = session.query(TopicIdea).order_by(TopicIdea.id).all()

        collection = compose_collection_from_events(
            session,
            [event.id for event in events],
        )
        result = get_topic_idea(session, collection.id)

        assert result is not None
        assert result.title == "今日2件事"
        assert result.recommended_format == "双事件组合型"


def test_compose_collection_allows_more_than_six_selected_events_with_warning_metadata():
    with make_session() as session:
        for index in range(7):
            add_hotspot(
                session,
                f"多事件{index}",
                source_platform=f"测试公开源{index}",
            )
        generate_local_daily_topic_candidates(session, max_hotspots=7, today_limit=5)
        events = session.query(TopicIdea).order_by(TopicIdea.id).all()

        collection = compose_collection_from_events(
            session,
            [event.id for event in events],
        )
        result = get_topic_idea(session, collection.id)

        assert result is not None
        assert result.title == "今日7件事"
        assert result.recommended_duration == "90-150 秒，建议精简"


def test_daily_generation_excludes_unverifiable_and_document_titles_from_pool():
    with make_session() as session:
        for index in range(5):
            add_hotspot(session, f"可信{index}")
        no_link = add_hotspot(session, "无链接", url="")
        document_title = add_hotspot(
            session,
            "报告",
            title="AI 行业报告.pdf 下载",
            url="https://example.com/reports/ai-report.pdf",
        )
        sample = add_hotspot(session, "示例", data_origin="sample")

        generate_local_daily_topic_candidates(session, max_hotspots=8, today_limit=5)
        result = list_topic_ideas(session)
        today_hotspot_ids = {
            item.hot_item_id for item in result.items if item.recommendation_bucket == "today"
        }
        backup_hotspot_ids = {
            item.hot_item_id for item in result.items if item.recommendation_bucket == "backup"
        }

        assert no_link.id not in today_hotspot_ids
        assert document_title.id not in today_hotspot_ids
        assert no_link.id not in backup_hotspot_ids
        assert document_title.id not in backup_hotspot_ids
        assert sample.id not in today_hotspot_ids
        assert sample.id not in backup_hotspot_ids


def test_expired_hotspots_archive_without_touching_selected_or_scripted_items():
    now = datetime(2026, 6, 19, 10, 0, tzinfo=timezone.utc)
    old_time = now - timedelta(hours=37)
    with make_session() as session:
        expired = add_hotspot(session, "过期", published_at=old_time)
        selected = add_hotspot(session, "已选", published_at=old_time)
        scripted = add_hotspot(session, "脚本", published_at=old_time)
        projected = add_hotspot(session, "项目", published_at=old_time)
        expired_idea = add_topic_idea(session, expired, "过期")
        selected_idea = add_topic_idea(session, selected, "已选", is_selected=True)
        scripted_idea = add_topic_idea(session, scripted, "脚本")
        projected_idea = add_topic_idea(session, projected, "项目")
        session.add(ScriptAsset(topic_idea_id=scripted_idea.id, voiceover="已生成口播稿"))
        session.add(
            ProductionPipeline(
                hotspot_id=projected.id,
                topic_idea_id=projected_idea.id,
                project_name="已生成项目",
            )
        )
        session.commit()

        result = archive_expired_hotspots(session, now=now)

        assert result.archived_count == 1
        assert session.get(HotItem, expired.id).lifecycle_status == "archived"
        assert session.get(HotItem, selected.id).lifecycle_status == "active"
        assert session.get(HotItem, scripted.id).lifecycle_status == "active"
        assert session.get(HotItem, projected.id).lifecycle_status == "active"
        pool = list_topic_ideas(session)
        visible_ids = {item.id for item in pool.items}
        assert expired_idea.id not in visible_ids
        assert selected_idea.id not in visible_ids
        assert scripted_idea.id not in visible_ids


def test_update_topic_recommendation_bucket_is_persistent():
    with make_session() as session:
        hotspot = add_hotspot(session, "池")
        idea = add_topic_idea(session, hotspot, "池")
        idea.generation_source = "local_rule"
        idea.source_hotspot_ids = [hotspot.id]
        idea.source_hotspot_titles = [hotspot.title]
        session.add(idea)
        session.commit()

        updated = update_topic_recommendation_bucket(session, idea.id, "today")

        assert updated is not None
        assert get_topic_idea(session, idea.id).recommendation_bucket == "today"


def test_update_topic_recommendation_rejects_unverified_topic_for_today():
    with make_session() as session:
        hotspot = add_hotspot(session, "无链接池", url="")
        idea = add_topic_idea(session, hotspot, "无链接池")
        idea.generation_source = "local_rule"
        idea.source_hotspot_ids = [hotspot.id]
        idea.source_hotspot_titles = [hotspot.title]
        session.add(idea)
        session.commit()

        updated = update_topic_recommendation_bucket(session, idea.id, "today")

        assert updated is not None
        assert get_topic_idea(session, idea.id).recommendation_bucket == "backup"


def test_topic_pool_serialization_keeps_sample_out_of_general_pool():
    with make_session() as session:
        hotspot = add_hotspot(session, "旧示例", data_origin="sample")
        idea = add_topic_idea(session, hotspot, "旧示例")
        idea.generation_source = "sample"
        idea.recommendation_bucket = "today"
        idea.is_example = True
        session.add(idea)
        session.commit()

        result = list_topic_ideas(session)

        assert result.items == []


def test_topic_idea_pool_api_and_select_endpoint():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    testing_session_local = sessionmaker(bind=engine)
    with testing_session_local() as setup_session:
        hotspot = add_hotspot(setup_session, "接口")
        idea = add_topic_idea(setup_session, hotspot, "接口")
        hotspot_id = hotspot.id
        idea_id = idea.id

    def override_get_db():
        db = testing_session_local()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        client = TestClient(app)
        list_response = client.get(
            "/api/topic-ideas",
            params={"hotspot_id": hotspot_id},
        )
        detail_response = client.get(f"/api/topic-ideas/{idea_id}")
        select_response = client.post(f"/api/topic-ideas/{idea_id}/select")
    finally:
        app.dependency_overrides.clear()

    assert list_response.status_code == 200
    assert list_response.json()["items"][0]["hotspot_title"] == "热点 接口"
    assert detail_response.status_code == 200
    assert select_response.status_code == 200
    assert select_response.json()["is_selected"] is True


def test_generate_script_endpoint_builds_fake_project(tmp_path, monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    testing_session_local = sessionmaker(bind=engine)
    with testing_session_local() as setup_session:
        hotspot = add_hotspot(setup_session, "生成")
        idea = add_topic_idea(setup_session, hotspot, "生成")
        idea_id = idea.id

    def override_get_db():
        db = testing_session_local()
        try:
            yield db
        finally:
            db.close()

    material_root = tmp_path / "短视频工厂"
    project_root = tmp_path / "项目"
    project_root.mkdir(parents=True)
    monkeypatch.setenv("MATERIAL_ROOT_DIR", str(material_root))
    monkeypatch.setenv("PROJECT_ROOT_DIR", str(project_root))
    from app.core.config import get_settings

    get_settings.cache_clear()
    app.dependency_overrides[get_db] = override_get_db
    try:
        client = TestClient(app)
        response = client.post(f"/api/topic-ideas/{idea_id}/generate-script")
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()

    assert response.status_code == 200
    data = response.json()
    assert data["provider"] == "fake"
    assert data["topic_idea_id"] == idea_id
    assert data["script_asset_id"] is not None
    assert data["pipeline_id"] is not None
    with testing_session_local() as verify_session:
        assert verify_session.query(ScriptAsset).count() == 1
        pipeline = verify_session.query(ProductionPipeline).one()
        assert pipeline.status == "需要人工确认"
        steps = {step.name: step.status for step in pipeline.steps}
        assert steps["生成脚本素材"] == "已完成"
        assert steps["生成素材任务"] == "已完成"
        assert steps["创建中文项目目录"] == "已完成"
        assert pipeline.project_dir
        assert Path(pipeline.project_dir).is_dir()
        assert all(
            generation.provider == "fake"
            for generation in verify_session.query(AiGeneration).all()
        )
