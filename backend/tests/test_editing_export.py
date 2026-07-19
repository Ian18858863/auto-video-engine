from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.main import app
from app.models import HotItem, ProductionPipeline
from app.pipeline.editing_export_service import (
    build_editing_export_package,
    serialize_editing_export_package,
)
from app.pipeline.service import build_project_from_hotspot
from app.pipeline.project_identity import PROJECT_REFERENCE_FILENAME, ProjectIdentityError
from app.pipeline.timeline_spec import TimelineSpecLedger


def make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return Session(engine)


def prepared_material_root(tmp_path: Path) -> Path:
    root = tmp_path / "短视频工厂"
    (root / "项目").mkdir(parents=True, exist_ok=True)
    return root


def add_hotspot(session: Session) -> HotItem:
    item = HotItem(
        source_platform="Hacker News",
        source_type="community",
        title="AI 自动化流程进入团队日常工作",
        url="https://example.com/editing-export",
        published_at=datetime.now(timezone.utc),
        summary="团队开始把 AI 自动化流程接入日常生产。",
        tags=["AI", "自动化"],
        category="AI",
        hot_score=88,
        topic_score=94,
        save_score=91,
    )
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


def test_build_editing_export_package_writes_timeline_srt_and_recommended_template(tmp_path):
    with make_session() as session:
        hotspot = add_hotspot(session)
        built = build_project_from_hotspot(
            session,
            hotspot.id,
            material_root=prepared_material_root(tmp_path),
        )
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        material_step = next(item for item in pipeline.steps if item.name == "生成素材任务")
        output_summary = copy.deepcopy(material_step.output_summary)
        output_summary["material_tasks"][0].update(
            {
                "evidence_kind": "atmosphere",
                "source_url": "https://videos.pexels.com/example.mp4",
                "is_locally_generated": False,
            }
        )
        material_step.output_summary = output_summary
        session.commit()

        package = build_editing_export_package(
            session,
            built.pipeline_id,
            project_root=prepared_material_root(tmp_path) / "项目",
        )
        data = serialize_editing_export_package(package)

        assert data["status"] == "已生成待确认"
        assert data["timeline"]
        assert len(data["versions"]) == 1
        assert data["recommended_template"]["name"] == "智能推荐模板"
        assert data["export_task"]["real_jianying_automation"] is False
        assert data["export_task"]["real_avatar_generation"] is False
        assert data["export_task"]["real_publish_executed"] is False
        assert data["timeline_spec"]["version_id"].startswith("ts-")
        assert data["export_task"]["timeline_version_id"] == data["timeline_spec"]["version_id"]
        assert Path(data["generated_files"]["timeline_json"]).is_file()
        assert Path(data["generated_files"]["subtitles_srt"]).is_file()
        assert Path(data["generated_files"]["rough_cut_plan_json"]).is_file()
        assert data["timeline"][0]["evidence_kind"] == "atmosphere"
        assert data["timeline"][0]["source_url"] == "https://videos.pexels.com/example.mp4"
        assert data["timeline"][0]["is_locally_generated"] is False
        timeline = json.loads(Path(data["generated_files"]["timeline_json"]).read_text(encoding="utf-8"))
        assert timeline["timeline"][0]["voiceover"]
        assert timeline["timeline_spec"]["version_id"] == data["timeline_spec"]["version_id"]
        assert timeline["timeline"][0]["evidence_kind"] == "atmosphere"
        assert timeline["timeline"][0]["source_url"] == "https://videos.pexels.com/example.mp4"
        assert timeline["timeline"][0]["is_locally_generated"] is False
        rough_cut = json.loads(
            Path(data["generated_files"]["rough_cut_plan_json"]).read_text(encoding="utf-8")
        )
        assert rough_cut["timeline"][0]["evidence_kind"] == "atmosphere"
        assert rough_cut["timeline"][0]["source_url"] == "https://videos.pexels.com/example.mp4"
        assert rough_cut["timeline"][0]["is_locally_generated"] is False


def test_read_only_package_marks_changed_shot_stale_until_new_timeline_is_written(
    tmp_path,
):
    with make_session() as session:
        hotspot = add_hotspot(session)
        built = build_project_from_hotspot(
            session,
            hotspot.id,
            material_root=prepared_material_root(tmp_path),
        )
        first = build_editing_export_package(
            session,
            built.pipeline_id,
            project_root=prepared_material_root(tmp_path) / "项目",
        )
        first_data = serialize_editing_export_package(first)
        first_version = first_data["timeline_spec"]["version_id"]
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        assert pipeline is not None
        material_step = next(item for item in pipeline.steps if item.name == "生成素材任务")
        payload = copy.deepcopy(material_step.output_summary)
        shot_id = payload["material_tasks"][0]["shot_id"]
        payload["material_tasks"][0]["recommended_visual"] = "changed approved scene"
        material_step.output_summary = payload
        session.commit()

        stale = serialize_editing_export_package(
            build_editing_export_package(
                session,
                built.pipeline_id,
                write_files=False,
                project_root=prepared_material_root(tmp_path) / "项目",
            )
        )

        assert stale["timeline_sync"]["status"] == "stale"
        assert stale["timeline_sync"]["current_version_id"] == first_version
        assert stale["timeline_sync"]["affected_segment_ids"] == [shot_id]

        rebuilt = serialize_editing_export_package(
            build_editing_export_package(
                session,
                built.pipeline_id,
                project_root=prepared_material_root(tmp_path) / "项目",
            )
        )
        assert rebuilt["timeline_sync"]["status"] == "current"
        assert rebuilt["timeline_spec"]["version_id"] != first_version
        assert rebuilt["timeline_spec"]["parent_version_id"] == first_version
        assert rebuilt["timeline_spec"]["change_set"]["affected_segment_ids"] == [shot_id]


def test_audio_first_binding_does_not_fake_a_full_edit_change(tmp_path):
    """Measured render timing is not a user edit and must not stale the editor."""

    with make_session() as session:
        project_root = prepared_material_root(tmp_path) / "项目"
        hotspot = add_hotspot(session)
        built = build_project_from_hotspot(
            session,
            hotspot.id,
            material_root=prepared_material_root(tmp_path),
        )
        first = serialize_editing_export_package(
            build_editing_export_package(
                session,
                built.pipeline_id,
                project_root=project_root,
            )
        )
        logical_version = first["timeline_spec"]["version_id"]
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        assert pipeline is not None
        ledger = TimelineSpecLedger(pipeline, project_root=project_root)
        logical = ledger.latest()
        assert logical is not None

        retimed_segments = copy.deepcopy(list(logical.segments))
        for index, segment in enumerate(retimed_segments):
            start = float(segment["start_seconds"]) * 0.5
            end = float(segment["end_seconds"]) * 0.5
            segment["start_seconds"] = start
            segment["end_seconds"] = end
            segment["start"] = f"{start:.3f}"
            segment["end"] = f"{end:.3f}"
        bound = ledger.create(
            {
                "tracks": copy.deepcopy(list(logical.tracks)),
                "segments": retimed_segments,
                "settings": copy.deepcopy(logical.settings),
                "artifacts": {},
            },
            source="audio_first_timing",
        )
        assert bound.parent_version_id == logical_version

        unchanged = serialize_editing_export_package(
            build_editing_export_package(
                session,
                built.pipeline_id,
                write_files=False,
                project_root=project_root,
            )
        )
        assert unchanged["timeline_sync"]["status"] == "current"
        assert unchanged["timeline_sync"]["current_version_id"] == bound.version_id
        assert unchanged["timeline_sync"]["affected_segment_ids"] == []

        material_step = next(item for item in pipeline.steps if item.name == "生成素材任务")
        payload = copy.deepcopy(material_step.output_summary)
        changed_shot_id = payload["material_tasks"][0]["shot_id"]
        payload["material_tasks"][0]["recommended_visual"] = "only this shot changed"
        material_step.output_summary = payload
        session.commit()

        changed = serialize_editing_export_package(
            build_editing_export_package(
                session,
                built.pipeline_id,
                write_files=False,
                project_root=project_root,
            )
        )
        assert changed["timeline_sync"]["status"] == "stale"
        assert changed["timeline_sync"]["current_version_id"] == bound.version_id
        assert changed["timeline_sync"]["affected_segment_ids"] == [changed_shot_id]

        rebuilt = serialize_editing_export_package(
            build_editing_export_package(
                session,
                built.pipeline_id,
                project_root=project_root,
            )
        )
        assert rebuilt["timeline_spec"]["parent_version_id"] == bound.version_id
        assert rebuilt["timeline_spec"]["change_set"]["change_base_version_id"] == logical_version
        assert rebuilt["timeline_spec"]["change_set"]["affected_segment_ids"] == [
            changed_shot_id
        ]


def test_storyboard_fallback_timeline_uses_empty_unverified_provenance(tmp_path):
    with make_session() as session:
        hotspot = add_hotspot(session)
        built = build_project_from_hotspot(
            session,
            hotspot.id,
            material_root=prepared_material_root(tmp_path),
        )
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        material_step = next(item for item in pipeline.steps if item.name == "生成素材任务")
        output_summary = copy.deepcopy(material_step.output_summary)
        output_summary["material_tasks"] = []
        material_step.output_summary = output_summary
        session.commit()

        package = build_editing_export_package(
            session,
            built.pipeline_id,
            project_root=prepared_material_root(tmp_path) / "项目",
        )
        data = serialize_editing_export_package(package)

        assert data["timeline"]
        assert data["timeline"][0]["evidence_kind"] == ""
        assert data["timeline"][0]["source_url"] == ""
        assert data["timeline"][0]["is_locally_generated"] is False
        timeline = json.loads(Path(data["generated_files"]["timeline_json"]).read_text(encoding="utf-8"))
        assert timeline["timeline"][0]["evidence_kind"] == ""
        assert timeline["timeline"][0]["source_url"] == ""
        assert timeline["timeline"][0]["is_locally_generated"] is False


def test_editing_export_rejects_tampered_project_reference_marker(tmp_path):
    material_root = tmp_path / "短视频工厂"
    project_root = material_root / "项目"
    project_root.mkdir(parents=True)
    with make_session() as session:
        hotspot = add_hotspot(session)
        built = build_project_from_hotspot(
            session,
            hotspot.id,
            material_root=material_root,
        )
        reference = Path(built.project_dir) / PROJECT_REFERENCE_FILENAME
        reference.write_text('{"project_key":"tampered"}', encoding="utf-8")

        with pytest.raises(ProjectIdentityError, match="reference|marker|tamper"):
            build_editing_export_package(
                session,
                built.pipeline_id,
                project_root=project_root,
            )


def test_editing_export_rejects_reparse_material_info_without_rewrite_or_state_change(
    tmp_path,
    monkeypatch,
):
    material_root = prepared_material_root(tmp_path)
    project_root = material_root / "项目"
    with make_session() as session:
        hotspot = add_hotspot(session)
        built = build_project_from_hotspot(
            session,
            hotspot.id,
            material_root=material_root,
        )
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        info_dir = Path(pipeline.project_dir) / "素材信息"
        timeline_path = info_dir / "timeline.json"
        before_bytes = timeline_path.read_bytes()
        step = next(item for item in pipeline.steps if item.name == "生成剪辑清单")
        before_state = copy.deepcopy(step.output_summary)
        identity = __import__("app.pipeline.project_identity", fromlist=["_is_reparse"])
        original_probe = identity._is_reparse

        def simulated_reparse(path: Path) -> bool:
            return Path(path) == info_dir or original_probe(path)

        monkeypatch.setattr(identity, "_is_reparse", simulated_reparse)

        with pytest.raises(ProjectIdentityError, match="reparse|junction|child"):
            build_editing_export_package(
                session,
                built.pipeline_id,
                project_root=project_root,
            )

        session.expire_all()
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        after_state = next(
            item for item in pipeline.steps if item.name == "生成剪辑清单"
        ).output_summary
        assert timeline_path.read_bytes() == before_bytes
        assert after_state == before_state


def test_editing_export_batch_failure_restores_all_previous_files_and_state(
    tmp_path,
    monkeypatch,
):
    material_root = prepared_material_root(tmp_path)
    project_root = material_root / "项目"
    with make_session() as session:
        hotspot = add_hotspot(session)
        built = build_project_from_hotspot(
            session,
            hotspot.id,
            material_root=material_root,
        )
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        info_dir = Path(pipeline.project_dir) / "素材信息"
        filenames = [
            "timeline.json",
            "rough_cut_plan.json",
            "subtitles.srt",
            "剪辑说明.md",
        ]
        before_files = {name: (info_dir / name).read_bytes() for name in filenames}
        step = next(item for item in pipeline.steps if item.name == "生成剪辑清单")
        before_state = copy.deepcopy(step.output_summary)
        identity = __import__("app.pipeline.project_identity", fromlist=["os"])
        original_replace = identity.os.replace
        commit_count = 0

        def fail_second_commit(source, destination):
            nonlocal commit_count
            if ".tmp-" in Path(source).name:
                commit_count += 1
                if commit_count == 2:
                    raise OSError("injected second-file commit failure")
            return original_replace(source, destination)

        monkeypatch.setattr(identity.os, "replace", fail_second_commit)

        with pytest.raises(ProjectIdentityError, match="batch|rolled back"):
            build_editing_export_package(
                session,
                built.pipeline_id,
                project_root=project_root,
            )

        session.expire_all()
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        after_state = next(
            item for item in pipeline.steps if item.name == "生成剪辑清单"
        ).output_summary
        assert {name: (info_dir / name).read_bytes() for name in filenames} == before_files
        assert after_state == before_state
        assert not list(info_dir.glob(".tmp-*"))
        assert not list(info_dir.glob(".rb-*"))


def test_editing_export_api_reads_safely_but_blocks_stale_script_material(tmp_path, monkeypatch):
    material_root = prepared_material_root(tmp_path)
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
            material_root=material_root,
        )
        pipeline_id = built.pipeline_id
        pipeline = setup_session.get(ProductionPipeline, pipeline_id)
        assert pipeline is not None
        assert pipeline.script_asset is not None
        pipeline.script_asset.voiceover = "黄金链新脚本，必须重建正式导出。"
        for step in pipeline.steps:
            if step.name in {
                "生成 Codex 图文任务",
                "生成数字人字段",
                "生成剪辑清单",
                "导出 script.md",
                "导出 shotlist.csv",
                "导出 edit_manifest.json",
            }:
                step.status = "未开始"
                step.error = ""
                step.output_summary = {}
                step.started_at = None
                step.ended_at = None
        setup_session.commit()

    def override_get_db():
        db = testing_session_local()
        try:
            yield db
        finally:
            db.close()

    from app.core.config import get_settings

    monkeypatch.setenv(
        "PROJECT_ROOT_DIR",
        str(material_root / "项目"),
    )
    monkeypatch.setenv("MATERIAL_ROOT_DIR", str(material_root))
    get_settings.cache_clear()
    app.dependency_overrides[get_db] = override_get_db
    try:
        client = TestClient(app)
        response = client.get(f"/api/pipeline/{pipeline_id}/editing-export")
        timeline_response = client.get(f"/api/pipeline/{pipeline_id}/timeline-spec")
        rebuild_response = client.post(f"/api/pipeline/{pipeline_id}/editing-export/rebuild")
        completion_response = client.get(f"/api/pipeline/{pipeline_id}/completion-status")
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()

    assert response.status_code == 200
    assert timeline_response.status_code == 200
    assert rebuild_response.status_code == 409, rebuild_response.text
    assert "素材锁定已过期" in rebuild_response.json()["detail"]
    assert completion_response.status_code == 200
    data = response.json()
    assert data["timeline"]
    assert timeline_response.json()["timeline_spec"]["version_id"].startswith("ts-")
    assert completion_response.json()["completion_reason_code"] == "approval_missing"
    with testing_session_local() as verify_session:
        pipeline = verify_session.get(ProductionPipeline, pipeline_id)
        assert pipeline is not None
        expected_blocked = {
            "生成 Codex 图文任务",
            "生成数字人字段",
            "生成剪辑清单",
            "导出 script.md",
            "导出 shotlist.csv",
            "导出 edit_manifest.json",
        }
        assert not {
            step.name for step in pipeline.steps if step.status == "已完成"
        }.intersection(expected_blocked)
