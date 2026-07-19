from datetime import datetime, timezone

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session

from app.core.database import Base
from app.models import HotItem


def test_core_database_tables_can_be_created():
    engine = create_engine("sqlite:///:memory:")

    Base.metadata.create_all(bind=engine)

    table_names = set(inspect(engine).get_table_names())
    assert {
        "hot_items",
        "topic_ideas",
        "ai_generations",
        "script_assets",
        "content_quality_checks",
        "copywriting_samples",
        "video_posts",
        "source_runs",
        "app_config",
    }.issubset(table_names)


def test_hot_item_can_be_inserted_and_read():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)

    published_at = datetime(2026, 6, 14, 9, 30, tzinfo=timezone.utc)
    with Session(engine) as session:
        hot_item = HotItem(
            source_platform="TechCrunch",
            source_type="news",
            title="AI 工具开始进入企业日常工作流",
            url="https://example.com/ai-workflow",
            published_at=published_at,
            summary="AI 工具正在从尝鲜转向真实工作流。",
            metrics={"rank": 3, "comments": 18},
            tags=["AI", "工具", "商业"],
            category="AI",
            hot_score=81.5,
            topic_score=88.0,
            save_score=76.0,
        )
        session.add(hot_item)
        session.commit()

    with Session(engine) as session:
        stored = session.query(HotItem).one()

    assert stored.source_platform == "TechCrunch"
    assert stored.source_type == "news"
    assert stored.title == "AI 工具开始进入企业日常工作流"
    assert stored.metrics == {"rank": 3, "comments": 18}
    assert stored.tags == ["AI", "工具", "商业"]
    assert stored.category == "AI"
    assert stored.is_filtered is False
    assert stored.is_favorite is False
    assert stored.has_topic_ideas is False
    assert stored.has_script is False
