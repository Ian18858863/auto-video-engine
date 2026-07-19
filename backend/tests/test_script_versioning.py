from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import threading

import pytest

from app.pipeline.script_versioning import (
    ScriptVersionLedger,
    ScriptVersionError,
)
from app.pipeline.shot_director import build_shot_plan
from app.models import ProductionPipeline
from app.pipeline.service import _load_approved_shot_plan, build_project_from_hotspot
from tests.test_production_pipeline import add_hot_item, make_session, prepared_material_root


def test_shot_director_uses_semantic_groups_instead_of_punctuation():
    plan = build_shot_plan(
        {
            "voiceover": "先看事实。再看影响。",
            "storyboard": [
                {
                    "shot_id": "shot-01",
                    "voiceover": "先看事实。再看影响。",
                    "semantic_role": "evidence",
                    "visual_intent": "展示官方页面中的原始数字",
                    "material_requirements": {"official_source_url": "https://example.com/report"},
                    "start": "00:00",
                    "end": "00:05",
                },
                {
                    "shot_id": "shot-02",
                    "voiceover": "这意味着团队会重新分配预算。",
                    "semantic_role": "evidence",
                    "visual_intent": "解释预算变化",
                    "start": "00:05",
                    "end": "00:10",
                },
                {
                    "shot_id": "shot-03",
                    "voiceover": "最后给一个判断。",
                    "semantic_role": "conclusion",
                    "visual_intent": "结论卡片",
                    "start": "00:10",
                    "end": "00:13",
                },
            ],
        }
    )

    assert len(plan) == 2
    assert plan[0]["beat_type"] == "evidence"
    assert "先看事实" in plan[0]["voiceover"]
    assert "预算" in plan[0]["voiceover"]
    assert plan[0]["evidence_kind"] == "direct_evidence"
    assert plan[0]["acceptance_conditions"]
    assert plan[1]["beat_type"] == "conclusion"


def test_shot_director_expands_explicit_semantic_beats_without_sentence_splitting():
    plan = build_shot_plan(
        {
            "voiceover": "一条口播对应两个观众动作。",
            "storyboard": [
                {
                    "shot_id": "shot-01",
                    "voiceover": "一条口播对应两个观众动作。",
                    "semantic_beats": [
                        {"beat_type": "hook", "viewer_takeaway": "先停下来"},
                        {"beat_type": "explanation", "viewer_takeaway": "再理解原因"},
                    ],
                    "visual_intent": "标题和解释卡片",
                    "start": "00:00",
                    "end": "00:06",
                }
            ],
        }
    )

    assert [item["beat_type"] for item in plan] == ["hook", "explanation"]
    assert all(item["shot_id"] == "shot-01" for item in plan)
    assert all(item["duration_seconds"] > 0 for item in plan)


def test_script_version_ledger_is_append_only_and_idempotent(tmp_path: Path):
    with make_session() as session:
        built = build_project_from_hotspot(
            session,
            add_hot_item(session).id,
            material_root=prepared_material_root(tmp_path),
        )
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        assert pipeline is not None
        ledger = ScriptVersionLedger(
            pipeline,
            project_root=prepared_material_root(tmp_path) / "项目",
        )
        payload = {
        "voiceover": "第一版口播",
        "storyboard": [{"shot_id": "shot-01", "voiceover": "第一版口播"}],
        "subtitles": [],
        "title_options": [],
        "cover_copy": [],
        "publish_copy": {},
        }

        first = ledger.create(payload, fact_sources=[{"url": "https://example.com/a"}])
        same = ledger.create(payload, fact_sources=[{"url": "https://example.com/a"}])
        second = ledger.create(
            {**payload, "voiceover": "第二版口播"},
            fact_sources=[{"url": "https://example.com/a"}],
        )

        assert same.version_id == first.version_id
        assert second.version_number == first.version_number + 1
        assert [item.version_id for item in ledger.list()] == [first.version_id, second.version_id]
        original = json.loads(
            (Path(pipeline.project_dir) / "脚本版本" / "versions" / f"{first.version_id}.json").read_text(
                encoding="utf-8"
            )
        )
        assert original["payload"]["voiceover"] == "第一版口播"

        approved = ledger.approve(first.version_id)
        assert approved.status == "approved"
        invalidated = ledger.invalidate(first.version_id, reason="事实来源已变化")
        assert invalidated.status == "invalidated"
        assert invalidated.invalidated_reason == "事实来源已变化"


def test_concurrent_script_versions_receive_unique_numbers_and_remain_readable(
    tmp_path: Path,
):
    with make_session() as session:
        material_root = prepared_material_root(tmp_path)
        built = build_project_from_hotspot(
            session,
            add_hot_item(session).id,
            material_root=material_root,
        )
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        assert pipeline is not None
        project_root = material_root / "项目"
        barrier = threading.Barrier(2)

        def create_version(index: int):
            barrier.wait(timeout=10)
            return ScriptVersionLedger(pipeline, project_root=project_root).create(
                {
                    "voiceover": f"并发版本 {index}",
                    "storyboard": [
                        {
                            "shot_id": "shot-01",
                            "voiceover": f"并发版本 {index}",
                        }
                    ],
                },
                source="concurrency_test",
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            snapshots = list(executor.map(create_version, (1, 2)))

        versions = ScriptVersionLedger(pipeline, project_root=project_root).list()
        created_ids = {item.version_id for item in snapshots}
        persisted = [item for item in versions if item.version_id in created_ids]

        assert len(created_ids) == 2
        assert len(persisted) == 2
        assert len({item.version_number for item in versions}) == len(versions)
        assert [item.version_number for item in versions] == list(
            range(1, len(versions) + 1)
        )


def test_script_version_ledger_rejects_non_project_root_or_tampered_snapshot(tmp_path: Path):
    with make_session() as session:
        built = build_project_from_hotspot(
            session,
            add_hot_item(session).id,
            material_root=prepared_material_root(tmp_path),
        )
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        assert pipeline is not None
        ledger = ScriptVersionLedger(
            pipeline,
            project_root=prepared_material_root(tmp_path) / "项目",
        )
        with pytest.raises(ScriptVersionError, match="脚本版本内容不能为空"):
            ledger.create({}, fact_sources=[])


def test_script_version_ledger_fails_closed_on_cross_project_or_malformed_shot_plan(
    tmp_path: Path,
):
    with make_session() as session:
        built = build_project_from_hotspot(
            session,
            add_hot_item(session).id,
            material_root=prepared_material_root(tmp_path),
        )
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        assert pipeline is not None
        root = prepared_material_root(tmp_path) / "项目"
        ledger = ScriptVersionLedger(pipeline, project_root=root)
        snapshot = ledger.create(
            {
                "voiceover": "可验证脚本",
                "storyboard": [{"shot_id": "shot-01", "voiceover": "可验证脚本"}],
            }
        )
        path = Path(pipeline.project_dir) / "脚本版本" / "versions" / f"{snapshot.version_id}.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["project_key"] = "another-project"
        path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        with pytest.raises(ScriptVersionError, match="项目身份|快照哈希不匹配"):
            ledger.list()


def test_v2_script_version_binds_entire_shot_plan_and_rejects_tampering(
    tmp_path: Path,
):
    with make_session() as session:
        built = build_project_from_hotspot(
            session,
            add_hot_item(session).id,
            material_root=prepared_material_root(tmp_path),
        )
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        assert pipeline is not None
        ledger = ScriptVersionLedger(
            pipeline,
            project_root=prepared_material_root(tmp_path) / "项目",
        )
        snapshot = ledger.create(
            {
                "voiceover": "可验证的完整分镜",
                "storyboard": [
                    {
                        "shot_id": "shot-01",
                        "voiceover": "可验证的完整分镜",
                        "semantic_role": "evidence",
                        "viewer_takeaway": "看懂原始证据",
                    }
                ],
            },
            input_evidence={"kind": "unit_test", "input_sha256": "a" * 64},
        )
        path = (
            Path(pipeline.project_dir)
            / "脚本版本"
            / "versions"
            / f"{snapshot.version_id}.json"
        )
        raw = json.loads(path.read_text(encoding="utf-8"))

        assert raw["schema_version"] == 2
        assert len(raw["shot_plan_hash"]) == 64
        assert len(raw["snapshot_hash"]) == 64
        assert snapshot.integrity_status == "full_envelope_verified"

        raw["shot_plan"][0]["viewer_takeaway"] = "被篡改的观众收获"
        path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

        with pytest.raises(ScriptVersionError, match="ShotPlan|快照哈希|哈希不匹配"):
            ledger.list()


def test_legacy_v1_snapshot_remains_readable_but_is_reported_as_partial_integrity(
    tmp_path: Path,
):
    with make_session() as session:
        built = build_project_from_hotspot(
            session,
            add_hot_item(session).id,
            material_root=prepared_material_root(tmp_path),
        )
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        assert pipeline is not None
        ledger = ScriptVersionLedger(
            pipeline,
            project_root=prepared_material_root(tmp_path) / "项目",
        )
        snapshot = ledger.create(
            {
                "voiceover": "旧版兼容脚本",
                "storyboard": [
                    {"shot_id": "shot-01", "voiceover": "旧版兼容脚本"}
                ],
            }
        )
        path = (
            Path(pipeline.project_dir)
            / "脚本版本"
            / "versions"
            / f"{snapshot.version_id}.json"
        )
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["schema_version"] = 1
        raw.pop("input_evidence", None)
        raw.pop("shot_plan_hash", None)
        raw.pop("snapshot_hash", None)
        legacy_input = {
            "payload": raw["payload"],
            "fact_sources": raw["fact_sources"],
        }
        raw["input_hash"] = hashlib.sha256(
            json.dumps(
                legacy_input,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

        restored = ledger.get(snapshot.version_id)

        assert restored.schema_version == 1
        assert restored.integrity_status == "legacy_payload_only"
        assert restored.shot_plan_hash == ""


def test_material_boundary_blocks_when_current_script_version_is_only_a_draft(
    tmp_path: Path,
):
    with make_session() as session:
        material_root = prepared_material_root(tmp_path)
        built = build_project_from_hotspot(
            session,
            add_hot_item(session).id,
            material_root=material_root,
        )
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        assert pipeline is not None
        ledger = ScriptVersionLedger(pipeline, project_root=material_root / "项目")
        approved = ledger.create(
            {
                "voiceover": "已批准脚本",
                "storyboard": [{"shot_id": "shot-01", "voiceover": "已批准脚本"}],
            }
        )
        ledger.approve(approved.version_id)
        draft = ledger.create(
            {
                "voiceover": "尚未批准的新草稿",
                "storyboard": [
                    {"shot_id": "shot-01", "voiceover": "尚未批准的新草稿"}
                ],
            },
        )
        script_step = next(
            step for step in pipeline.steps if step.name == "生成脚本素材"
        )
        output = dict(script_step.output_summary or {})
        factory = dict(output.get("script_factory") or {})
        factory["script_version_id"] = draft.version_id
        output["script_factory"] = factory
        script_step.output_summary = output
        session.add(script_step)
        session.commit()
        ledger.invalidate(approved.version_id, reason="新草稿替代旧批准")

        with pytest.raises(ScriptVersionError, match="当前脚本版本尚未批准"):
            _load_approved_shot_plan(pipeline, project_root=material_root / "项目")


def test_material_boundary_rejects_legacy_snapshot_without_shot_plan_hash(
    tmp_path: Path,
):
    with make_session() as session:
        material_root = prepared_material_root(tmp_path)
        built = build_project_from_hotspot(
            session,
            add_hot_item(session).id,
            material_root=material_root,
        )
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        assert pipeline is not None
        ledger = ScriptVersionLedger(pipeline, project_root=material_root / "项目")
        snapshot = ledger.create(
            {
                "voiceover": "旧版已批准脚本",
                "storyboard": [
                    {"shot_id": "shot-01", "voiceover": "旧版已批准脚本"}
                ],
            }
        )
        path = (
            Path(pipeline.project_dir)
            / "脚本版本"
            / "versions"
            / f"{snapshot.version_id}.json"
        )
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["schema_version"] = 1
        raw.pop("input_evidence", None)
        raw.pop("shot_plan_hash", None)
        raw.pop("snapshot_hash", None)
        raw["input_hash"] = hashlib.sha256(
            json.dumps(
                {
                    "payload": raw["payload"],
                    "fact_sources": raw["fact_sources"],
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        ledger.approve(snapshot.version_id)

        with pytest.raises(ScriptVersionError, match="ShotPlan.*完整哈希|旧版"):
            _load_approved_shot_plan(pipeline, project_root=material_root / "项目")


def test_approval_event_binds_full_snapshot_hash_and_rejects_tampering(
    tmp_path: Path,
):
    with make_session() as session:
        material_root = prepared_material_root(tmp_path)
        built = build_project_from_hotspot(
            session,
            add_hot_item(session).id,
            material_root=material_root,
        )
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        assert pipeline is not None
        ledger = ScriptVersionLedger(pipeline, project_root=material_root / "项目")
        snapshot = ledger.create(
            {
                "voiceover": "批准事件绑定测试",
                "storyboard": [
                    {"shot_id": "shot-01", "voiceover": "批准事件绑定测试"}
                ],
            }
        )
        approved = ledger.approve(snapshot.version_id)
        event_path = next(
            (Path(pipeline.project_dir) / "脚本版本" / "events").glob(
                "event-approved-*.json"
            )
        )
        raw = json.loads(event_path.read_text(encoding="utf-8"))

        assert raw["schema_version"] == 2
        assert raw["version_content_hash"] == snapshot.content_hash
        assert raw["version_snapshot_hash"] == snapshot.snapshot_hash
        assert len(raw["event_hash"]) == 64
        assert approved.approval_integrity_status == "snapshot_hash_bound"

        raw["at"] = "2099-01-01T00:00:00+00:00"
        event_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

        with pytest.raises(ScriptVersionError, match="事件哈希不匹配"):
            ledger.get(snapshot.version_id)


def test_material_boundary_rejects_legacy_approval_not_bound_to_snapshot_hash(
    tmp_path: Path,
):
    with make_session() as session:
        material_root = prepared_material_root(tmp_path)
        built = build_project_from_hotspot(
            session,
            add_hot_item(session).id,
            material_root=material_root,
        )
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        assert pipeline is not None
        ledger = ScriptVersionLedger(pipeline, project_root=material_root / "项目")
        snapshot = ledger.create(
            {
                "voiceover": "旧批准事件测试",
                "storyboard": [
                    {"shot_id": "shot-01", "voiceover": "旧批准事件测试"}
                ],
            }
        )
        ledger.approve(snapshot.version_id)
        event_path = next(
            (Path(pipeline.project_dir) / "脚本版本" / "events").glob(
                "event-approved-*.json"
            )
        )
        current = json.loads(event_path.read_text(encoding="utf-8"))
        event_path.write_text(
            json.dumps(
                {
                    "event": "approved",
                    "version_id": snapshot.version_id,
                    "at": current["at"],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        restored = ledger.get(snapshot.version_id)
        assert restored.status == "approved"
        assert restored.approval_integrity_status == "legacy_version_id_only"
        with pytest.raises(ScriptVersionError, match="批准.*完整快照哈希"):
            _load_approved_shot_plan(pipeline, project_root=material_root / "项目")


def test_script_version_operation_log_is_append_only_hashed_and_idempotent(
    tmp_path: Path,
):
    with make_session() as session:
        material_root = prepared_material_root(tmp_path)
        built = build_project_from_hotspot(
            session,
            add_hot_item(session).id,
            material_root=material_root,
        )
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        assert pipeline is not None
        ledger = ScriptVersionLedger(pipeline, project_root=material_root / "项目")
        snapshot = ledger.create(
            {
                "voiceover": "operation outbox test",
                "storyboard": [
                    {"shot_id": "shot-01", "voiceover": "operation outbox test"}
                ],
            }
        )
        operation_id = "a" * 32
        context = {
            "version_snapshot_hash": snapshot.snapshot_hash,
            "previous_approved_version_ids": [],
        }

        first = ledger.begin_operation(
            operation_id=operation_id,
            kind="approve",
            version_id=snapshot.version_id,
            context=context,
        )
        repeated = ledger.begin_operation(
            operation_id=operation_id,
            kind="approve",
            version_id=snapshot.version_id,
            context=context,
        )

        assert first == repeated
        assert first.status == "pending"
        assert len(first.intent_hash) == 64
        assert [item.operation_id for item in ledger.pending_operations()] == [
            operation_id
        ]

        completed = ledger.finish_operation(
            operation_id,
            outcome="completed",
            result={"database_state": "matched"},
        )
        repeated_completed = ledger.finish_operation(
            operation_id,
            outcome="completed",
            result={"database_state": "matched"},
        )

        assert completed == repeated_completed
        assert completed.status == "completed"
        assert ledger.pending_operations() == []
        operation_dir = Path(pipeline.project_dir) / "脚本版本" / "operations"
        assert len(list(operation_dir.glob("operation-*-intent.json"))) == 1
        assert len(list(operation_dir.glob("operation-*-outcome-*.json"))) == 1

        intent_path = next(operation_dir.glob("operation-*-intent.json"))
        tampered = json.loads(intent_path.read_text(encoding="utf-8"))
        tampered["context"]["version_snapshot_hash"] = "0" * 64
        intent_path.write_text(
            json.dumps(tampered, ensure_ascii=False),
            encoding="utf-8",
        )
        with pytest.raises(ScriptVersionError, match="operation|操作|哈希"):
            ledger.pending_operations()
