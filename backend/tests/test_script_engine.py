from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.main import app
from app.models import CopywritingSample
from app.script_engine.service import (
    get_script_engine_context,
    list_script_engine_samples,
    serialize_script_engine_sample_list,
)


def _payload() -> dict:
    return {
        "title": "脚本结构样本",
        "sample_type": "商业逻辑拆解表达",
        "sample_text": "表面事件 → 赚钱逻辑 → 谁受影响 → 普通人判断标准",
        "scene": "商业逻辑拆解",
        "content_direction": "AI 工具",
        "structure_type": "商业逻辑拆解型",
        "why_reference": "结构清楚，适合小白听懂。",
        "reusable_points": "先说表面，再拆底层逻辑。",
        "avoid_points": "不能直接复制原文。",
        "tags": ["脚本", "结构"],
        "enabled": True,
        "is_key_reference": True,
        "notes": "脚本引擎测试。",
    }


def test_script_engine_api_uses_new_route_and_old_table_for_compatibility():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    testing_session_local = sessionmaker(bind=engine)

    def override_get_db():
        db = testing_session_local()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        client = TestClient(app)
        create_response = client.post("/api/script-engine", json=_payload())
        sample_id = create_response.json()["id"]
        list_response = client.get("/api/script-engine?keyword=表面事件")
        old_list_response = client.get("/api/copywriting-samples?keyword=表面事件")
        detail_response = client.get(f"/api/script-engine/{sample_id}")
    finally:
        app.dependency_overrides.clear()

    assert create_response.status_code == 200
    assert list_response.status_code == 200
    assert list_response.json()["engine"] == "script_engine"
    assert list_response.json()["items"][0]["engine"] == "script_engine"
    assert "脚本引擎" in list_response.json()["style_priority"]
    assert old_list_response.status_code == 200
    assert old_list_response.json()["items"][0]["title"] == "脚本结构样本"
    assert detail_response.json()["sample_text"].startswith("表面事件")
    with testing_session_local() as session:
        assert session.query(CopywritingSample).count() == 1
        context = get_script_engine_context(session)
        assert context["source"] == "script_engine"
        assert context["samples"][0]["structure_type"] == "商业逻辑拆解型"
        serialized = serialize_script_engine_sample_list(list_script_engine_samples(session))
        assert serialized["engine"] == "script_engine"
