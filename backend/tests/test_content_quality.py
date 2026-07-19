from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.account_style.service import get_account_style_library, save_account_style_library
from app.content_quality.service import (
    QualityCheckPayload,
    check_manual_content,
    check_pipeline_script,
    run_local_quality_rules,
)
from app.core.database import Base, get_db
from app.main import app
from app.models import AiGeneration, ContentQualityCheck, HotItem
from app.pipeline.service import build_project_from_hotspot


def make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return Session(engine)


def add_hotspot(session: Session) -> HotItem:
    item = HotItem(
        source_platform="手动导入",
        source_type="manual",
        title="AI 工具进入企业采购流程",
        url="https://example.com/content-quality",
        published_at=datetime.now(timezone.utc),
        summary="企业开始把 AI 工具放进真实预算。",
        tags=["AI", "商业"],
        category="AI",
        hot_score=88,
        topic_score=95,
        save_score=90,
    )
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


def test_local_quality_rules_detect_banned_terms_common_phrases_and_investment_risk():
    with make_session() as session:
        result = run_local_quality_rules(
            session,
            "大家都知道，这个 AI 风口炸裂，是财富密码，现在可以买，马上上车。",
        )

        assert result["publish_suggestion"] == "不建议发布"
        assert result["risk_level"] == "高"
        assert "大家都知道" in result["matched_banned_expressions"]
        assert "炸裂" in result["matched_banned_expressions"]
        assert result["total_score"] < 70
        assert any("投资建议风险" in issue for issue in result["issues"])
        assert result["real_ai_called"] is False


def test_local_quality_rules_detect_unexplained_professional_terms():
    with make_session() as session:
        result = run_local_quality_rules(
            session,
            "这次宏观环境变化背后，是流动性收紧和平台生态变化，关键是看商业模式。",
        )

        assert "流动性收紧" in result["professional_terms_need_explanation"]
        assert "商业模式" in result["professional_terms_need_explanation"]
        assert result["risk_level"] == "中"
        assert result["publish_suggestion"] in {"建议修改", "不建议发布"}


def test_local_quality_rules_read_latest_account_style_config():
    with make_session() as session:
        style = get_account_style_library(session)
        style["banned_expressions"].append(
            {
                "id": "custom_bad",
                "label": "神级机会",
                "category": "自定义",
                "enabled": True,
            }
        )
        style["structure_preferences"][0]["framework"] = "测试结构：表面事件 → 判断标准"
        style["opening_templates"][0]["template"] = "测试开头：先讲普通人该看什么。"
        style["ending_templates"][0]["template"] = "测试结尾：给一个可复用判断标准。"
        save_account_style_library(session, style)

        result = run_local_quality_rules(
            session,
            "这个神级机会背后其实是平台分发入口变化，普通人要看判断标准。",
        )

        assert "神级机会" in result["matched_banned_expressions"]
        assert result["style_rules_used"]["structure_preferences"][0]["framework"] == (
            "测试结构：表面事件 → 判断标准"
        )
        assert result["style_rules_used"]["opening_templates"][0]["template"] == (
            "测试开头：先讲普通人该看什么。"
        )
        assert result["style_rules_used"]["ending_templates"][0]["template"] == (
            "测试结尾：给一个可复用判断标准。"
        )
        assert "内容结构偏好" in result["checked_dimensions"]
        assert "开头偏好" in result["checked_dimensions"]
        assert "结尾偏好" in result["checked_dimensions"]


def test_manual_quality_check_saves_record_without_ai_call():
    with make_session() as session:
        before = session.query(AiGeneration).count()
        record = check_manual_content(
            session,
            QualityCheckPayload(
                checked_text="说白了，商业模式就是它到底靠什么赚钱。普通人可以看三个判断标准。",
                source_title="手动测试",
                source_type="手动粘贴",
            ),
        )

        assert record.id is not None
        assert record.source_title == "手动测试"
        assert record.result["publish_suggestion"] in {"可以发布", "建议修改"}
        assert session.query(ContentQualityCheck).count() == 1
        assert session.query(AiGeneration).count() == before


def test_pipeline_script_quality_check_saves_record():
    with make_session() as session:
        hotspot = add_hotspot(session)
        built = build_project_from_hotspot(
            session,
            hotspot.id,
            material_root=_material_root(),
        )
        before = session.query(AiGeneration).count()

        record = check_pipeline_script(session, built.pipeline_id)

        assert record.pipeline_id == built.pipeline_id
        assert record.script_asset_id == built.script_asset_id
        assert record.source_type == "脚本工作台"
        assert record.result["style_source"] == "账号风格库"
        assert session.query(AiGeneration).count() == before


def test_content_quality_api_is_local_rules_only():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    testing_session_local = sessionmaker(bind=engine)
    with testing_session_local() as setup_session:
        hotspot = add_hotspot(setup_session)
        built = build_project_from_hotspot(
            setup_session,
            hotspot.id,
            material_root=_material_root(),
        )
        pipeline_id = built.pipeline_id
        generation_count = setup_session.query(AiGeneration).count()

    def override_get_db():
        db = testing_session_local()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        client = TestClient(app)
        manual_response = client.post(
            "/api/content-quality/manual/check",
            json={
                "text": "显而易见，这个风口全网疯传，但缺少判断标准。",
                "source_title": "手动接口测试",
            },
        )
        pipeline_response = client.post(
            f"/api/content-quality/pipeline/{pipeline_id}/check"
        )
    finally:
        app.dependency_overrides.clear()

    assert manual_response.status_code == 200
    assert "显而易见" in manual_response.json()["matched_banned_expressions"]
    assert manual_response.json()["local_rules_only"] is True
    assert pipeline_response.status_code == 200
    assert pipeline_response.json()["pipeline_id"] == pipeline_id
    with testing_session_local() as verify_session:
        assert verify_session.query(ContentQualityCheck).count() == 2
        assert verify_session.query(AiGeneration).count() == generation_count


def _material_root() -> Path:
    path = Path(".test-tmp") / "trend-video-factory-tests" / f"content-quality-{uuid4().hex}"
    path.mkdir(parents=True, exist_ok=True)
    return path
