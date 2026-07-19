from __future__ import annotations

import copy
import hashlib
import importlib
import importlib.util
import json
import os
import struct
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.models import ProductionPipeline
from app.pipeline.models import PIPELINE_STEP_NAMES


def _policy():
    spec = importlib.util.find_spec("app.pipeline.completion_policy")
    assert spec is not None, "Task 6 completion policy module is missing"
    return importlib.import_module("app.pipeline.completion_policy")


def _script(*, script_id: int = 20, topic_idea_id: int = 10):
    return SimpleNamespace(
        id=script_id,
        topic_idea_id=topic_idea_id,
        voiceover="这是当前可变口播。",
        storyboard=[{"shot_id": "shot-01", "voiceover": "第一镜"}],
        subtitles=[{"start": "00:00", "end": "00:03", "text": "第一镜"}],
        animation_prompts=[{"shot_id": "shot-01", "prompt": "数据流"}],
        title_options=[{"title": "可信标题"}],
        cover_copy=[{"text": "封面字"}],
        publish_copy={"douyin": "发布文案"},
        edit_checklist=[{"item": "事实来源", "checked": True}],
    )


def _pipeline(
    statuses: list[str] | None = None,
    *,
    stored_status: str = "进行中",
):
    statuses = statuses or ["已完成"] * len(PIPELINE_STEP_NAMES)
    steps = []
    for index, (name, status) in enumerate(
        zip(PIPELINE_STEP_NAMES, statuses, strict=True),
        start=1,
    ):
        summary: dict = {}
        if name == "选择候选选题" and status == "已完成":
            summary = {"topic_idea_id": 10}
        elif name == "生成脚本素材" and status == "已完成":
            summary = {"script_asset_id": 20, "from_cache": False}
        steps.append(
            SimpleNamespace(
                id=index,
                name=name,
                step_order=index,
                status=status,
                error="失败原因" if status == "失败" else "",
                output_summary=summary,
                started_at=None,
                ended_at=None,
            )
        )
    return SimpleNamespace(
        id=1,
        hotspot_id=7,
        topic_idea_id=10,
        script_asset_id=20,
        project_name="Task 6 临时项目",
        project_key="p-1-task-6",
        project_dir="",
        legacy_project_dir=None,
        status=stored_status,
        provider="fake",
        model="fake-low-cost",
        steps=steps,
        script_asset=_script(),
        completion_evidence={},
        created_at=datetime(2026, 7, 12, tzinfo=timezone.utc),
        updated_at=datetime(2026, 7, 12, tzinfo=timezone.utc),
    )


def _step(pipeline, name: str):
    return next(item for item in pipeline.steps if item.name == name)


NOW = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)
APPROVED_AT = datetime(2026, 7, 12, 11, 0, tzinfo=timezone.utc)


def _box(kind: bytes, payload: bytes = b"") -> bytes:
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def _valid_mp4() -> bytes:
    return (
        _box(b"ftyp", b"isom\x00\x00\x02\x00isom")
        + _box(b"moov", _box(b"mvhd"))
        + _box(b"mdat", b"verified-frame-data")
    )


def _json_bytes(payload: dict) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _deep_json_value(depth: int = 500):
    return json.loads("[" * depth + "0" + "]" * depth)


def _approved_project(tmp_path: Path):
    policy = _policy()
    identity = importlib.import_module("app.pipeline.project_identity")
    pipeline = _pipeline()
    project_root = tmp_path / "approved-project-root"
    project_root.mkdir()
    prepared = identity.prepare_pipeline_project_directory(
        pipeline,
        project_root=project_root,
        system_drive="C:",
    )
    finished_use = identity.begin_prepared_project_child_directory_use(
        pipeline,
        project_root=project_root,
        relative_path="成片",
        create=True,
        system_drive="C:",
    )
    artifact_path = finished_use.path / "final.mp4"
    report_path = finished_use.path / "quality-report.json"
    artifact = _valid_mp4()
    artifact_path.write_bytes(artifact)
    state_hash = policy.pipeline_state_sha256(pipeline)
    report = {
        "report_format": "auto-video-engine-quality-report-v1",
        "pipeline_id": pipeline.id,
        "pipeline_state_sha256": state_hash,
        "artifact_sha256": _sha256(artifact),
        "generated_at": (APPROVED_AT - timedelta(minutes=1)).isoformat(),
        "gates": {
            gate: {"status": "passed", "evidence": [f"{gate} 已检查"]}
            for gate in (
                "structure",
                "technical",
                "visual_narration",
                "facts_rights",
                "quality_contract",
            )
        },
        "release_blockers": [],
    }
    report_bytes = _json_bytes(report)
    report_path.write_bytes(report_bytes)
    pipeline.completion_evidence = {
        "evidence_version": 1,
        "pipeline_state_sha256": state_hash,
        "artifact_relative_path": "成片/final.mp4",
        "artifact_sha256": _sha256(artifact),
        "quality_report_relative_path": "成片/quality-report.json",
        "quality_report_sha256": _sha256(report_bytes),
        "approved_at": APPROVED_AT.isoformat(),
        "approved_by": "Ian",
    }
    assert Path(pipeline.project_dir) == prepared
    return pipeline, artifact_path, report_path, report


def _rewrite_report(pipeline, report_path: Path, report: dict) -> bytes:
    content = _json_bytes(report)
    report_path.write_bytes(content)
    pipeline.completion_evidence["quality_report_sha256"] = _sha256(content)
    return content


def _rewrite_artifact_and_bind_report(
    pipeline,
    artifact_path: Path,
    report_path: Path,
    report: dict,
    content: bytes,
) -> None:
    artifact_path.write_bytes(content)
    digest = _sha256(content)
    pipeline.completion_evidence["artifact_sha256"] = digest
    report["artifact_sha256"] = digest
    _rewrite_report(pipeline, report_path, report)


def _compact_json(value: dict) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _bind_quality_contract_v2(pipeline, report_path: Path, report: dict) -> Path:
    contract = {
        "format": "auto-video-engine-quality-contract-v1",
        "contract_id": f"qc-{pipeline.project_key}-r1",
        "pipeline_id": pipeline.id,
        "project_key": pipeline.project_key,
        "baseline": "r1-unified",
        "target": {
            "platform": "抖音",
            "aspect_ratio": "9:16",
            "duration_seconds": 60,
        },
        "reference_pack": {"status": "none_supplied", "items": []},
        "must_preserve": [
            {
                "id": "facts_traceable",
                "label": "事实可追溯",
                "criterion": "每个关键事实都能追溯到原始证据",
            }
        ],
        "allowed_differences": [
            {"id": "audio_first_timing", "label": "允许按旁白实测时长调整"}
        ],
        "planned_exceed_dimensions": [
            {
                "id": "information_clarity",
                "label": "信息清晰度",
                "criterion": "比统一 R1 基线更容易理解",
                "required_result": "exceeded",
            }
        ],
        "created_at": pipeline.created_at.isoformat(),
        "source": "master-refactor-phase1-unified-baseline",
    }
    contract_dir = Path(pipeline.project_dir) / "质量合同"
    contract_dir.mkdir(parents=True, exist_ok=True)
    contract_path = contract_dir / "quality-contract-v1.json"
    contract_bytes = _json_bytes(contract)
    contract_path.write_bytes(contract_bytes)
    report["gates"]["quality_contract"] = {
        "status": "passed",
        "evidence": [
            f"quality_contract_id:{contract['contract_id']}",
            f"quality_contract_sha256:{_sha256(contract_bytes)}",
            "quality_contract_path:质量合同/quality-contract-v1.json",
            "reference_pack:none_supplied",
            "must_preserve_review:"
            + _compact_json(
                {
                    "id": "facts_traceable",
                    "result": "passed",
                    "evidence": "已逐项检查事实来源",
                }
            ),
            "allowed_difference_review:"
            + _compact_json(
                {
                    "id": "audio_first_timing",
                    "result": "not_used",
                    "actual_difference": "",
                    "evidence": "本片未使用此允许差异",
                }
            ),
            "planned_exceed_review:"
            + _compact_json(
                {
                    "id": "information_clarity",
                    "result": "exceeded",
                    "difference": "信息层级更明确",
                    "evidence": "Ian 已逐项比较",
                }
            ),
        ],
    }
    pipeline.completion_evidence["evidence_version"] = 2
    _rewrite_report(pipeline, report_path, report)
    return contract_path


def _rewrite_contract_and_binding(
    pipeline,
    contract_path: Path,
    report_path: Path,
    report: dict,
    contract: dict,
) -> None:
    content = _json_bytes(contract)
    contract_path.write_bytes(content)
    reference_status = contract["reference_pack"]["status"]
    report["gates"]["quality_contract"]["evidence"] = [
        (
            f"quality_contract_sha256:{_sha256(content)}"
            if item.startswith("quality_contract_sha256:")
            else f"reference_pack:{reference_status}"
            if item.startswith("reference_pack:")
            else item
        )
        for item in report["gates"]["quality_contract"]["evidence"]
    ]
    _rewrite_report(pipeline, report_path, report)


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "duplicate",
        "out_of_order",
        "unknown_name",
        "unknown_status",
        "non_dict_summary",
        "non_json_summary",
        "non_finite_summary",
    ],
)
def test_preparation_invalid_structure_fails_closed(mutation: str) -> None:
    policy = _policy()
    pipeline = _pipeline()
    if mutation == "missing":
        pipeline.steps.pop()
    elif mutation == "duplicate":
        pipeline.steps[-1].name = pipeline.steps[-2].name
    elif mutation == "out_of_order":
        pipeline.steps[1].step_order = 99
    elif mutation == "unknown_name":
        pipeline.steps[-1].name = "不存在的步骤"
    elif mutation == "unknown_status":
        pipeline.steps[-1].status = "看起来完成"
    elif mutation == "non_dict_summary":
        pipeline.steps[-1].output_summary = []
    elif mutation == "non_json_summary":
        pipeline.steps[-1].output_summary = {"bad": {"not-json"}}
    elif mutation == "non_finite_summary":
        pipeline.steps[-1].output_summary = {"score": float("nan")}

    decision = policy.evaluate_pipeline_completion(pipeline)

    assert decision.is_complete is False
    assert decision.status == "需要人工确认"
    assert decision.reason_code == "preparation_invalid"
    assert decision.next_surface == "project"
    assert decision.needs_confirmation is True


def test_step_failed_has_priority_and_stored_complete_cannot_override() -> None:
    policy = _policy()
    statuses = ["已完成"] * len(PIPELINE_STEP_NAMES)
    statuses[5] = "进行中"
    statuses[3] = "失败"
    pipeline = _pipeline(statuses, stored_status="已完成")

    decision = policy.evaluate_pipeline_completion(pipeline)

    assert decision.is_complete is False
    assert decision.status == "失败"
    assert decision.reason_code == "step_failed"
    assert decision.current_step == PIPELINE_STEP_NAMES[3]
    assert decision.next_surface == "project"
    assert decision.needs_confirmation is True


def test_step_running_is_not_waiting_for_ian() -> None:
    policy = _policy()
    statuses = ["已完成"] * len(PIPELINE_STEP_NAMES)
    statuses[4] = "进行中"

    decision = policy.evaluate_pipeline_completion(_pipeline(statuses))

    assert decision.status == "进行中"
    assert decision.reason_code == "step_running"
    assert decision.current_step == PIPELINE_STEP_NAMES[4]
    assert decision.next_surface == "project"
    assert decision.needs_confirmation is False


def test_step_confirmation_required_has_priority_after_running() -> None:
    policy = _policy()
    statuses = ["已完成"] * len(PIPELINE_STEP_NAMES)
    statuses[6] = "需要人工确认"

    decision = policy.evaluate_pipeline_completion(_pipeline(statuses))

    assert decision.status == "需要人工确认"
    assert decision.reason_code == "step_confirmation_required"
    assert decision.current_step == PIPELINE_STEP_NAMES[6]
    assert decision.next_surface == "project"
    assert decision.needs_confirmation is True


def test_preparation_not_started_when_all_steps_are_untouched() -> None:
    policy = _policy()
    pipeline = _pipeline(["未开始"] * len(PIPELINE_STEP_NAMES))

    decision = policy.evaluate_pipeline_completion(pipeline)

    assert decision.status == "未开始"
    assert decision.reason_code == "preparation_not_started"
    assert decision.current_step == PIPELINE_STEP_NAMES[0]
    assert decision.next_surface == "project"
    assert decision.needs_confirmation is False


def test_preparation_incomplete_for_completed_and_unstarted_mixture() -> None:
    policy = _policy()
    statuses = ["已完成"] * 4 + ["未开始"] * (len(PIPELINE_STEP_NAMES) - 4)

    decision = policy.evaluate_pipeline_completion(_pipeline(statuses))

    assert decision.status == "进行中"
    assert decision.reason_code == "preparation_incomplete"
    assert decision.current_step == PIPELINE_STEP_NAMES[4]
    assert decision.next_surface == "project"
    assert decision.needs_confirmation is False


def test_approval_missing_after_all_preparation_is_complete() -> None:
    policy = _policy()
    prepared_pipeline = _pipeline()

    decision = policy.evaluate_pipeline_completion(prepared_pipeline)

    assert decision.is_complete is False
    assert decision.status == "需要人工确认"
    assert decision.reason_code == "approval_missing"
    assert decision.next_surface == "finished"
    assert decision.current_step == "成片验收"
    assert decision.next_action == "打开成片页检查当前文件和验收状态"
    assert decision.needs_confirmation is True


@pytest.mark.parametrize(
    ("step_name", "summary", "pipeline_field", "pipeline_value"),
    [
        ("选择候选选题", {}, "topic_idea_id", 10),
        ("选择候选选题", {"topic_idea_id": 11}, "topic_idea_id", 10),
        ("生成脚本素材", {}, "script_asset_id", 20),
        ("生成脚本素材", {"script_asset_id": 21}, "script_asset_id", 20),
    ],
)
def test_preparation_rejects_missing_or_mismatched_canonical_output_id(
    step_name: str,
    summary: dict,
    pipeline_field: str,
    pipeline_value: int,
) -> None:
    policy = _policy()
    pipeline = _pipeline()
    setattr(pipeline, pipeline_field, pipeline_value)
    _step(pipeline, step_name).output_summary = summary

    decision = policy.evaluate_pipeline_completion(pipeline)

    assert decision.reason_code == "preparation_invalid"


def test_script_step_cannot_hide_wrong_script_id_behind_topic_id() -> None:
    policy = _policy()
    pipeline = _pipeline()
    _step(pipeline, "生成脚本素材").output_summary = {
        "topic_idea_id": pipeline.script_asset_id,
        "script_asset_id": 999,
    }

    decision = policy.evaluate_pipeline_completion(pipeline)

    assert decision.reason_code == "preparation_invalid"


def test_completed_topic_step_rejects_pipeline_and_summary_ids_both_none() -> None:
    policy = _policy()
    pipeline = _pipeline()
    pipeline.topic_idea_id = None
    pipeline.script_asset.topic_idea_id = None
    _step(pipeline, "选择候选选题").output_summary = {"topic_idea_id": None}

    decision = policy.evaluate_pipeline_completion(pipeline)

    assert decision.reason_code == "preparation_invalid"
    assert decision.next_surface == "project"


def test_completed_script_step_rejects_pipeline_and_summary_ids_both_none() -> None:
    policy = _policy()
    pipeline = _pipeline()
    pipeline.script_asset_id = None
    pipeline.script_asset = None
    _step(pipeline, "生成脚本素材").output_summary = {"script_asset_id": None}

    decision = policy.evaluate_pipeline_completion(pipeline)

    assert decision.reason_code == "preparation_invalid"
    assert decision.next_surface == "project"


def test_deep_step_summary_fails_closed_without_recursion_error() -> None:
    policy = _policy()
    pipeline = _pipeline()
    _step(pipeline, "生成素材任务").output_summary = {"deep": _deep_json_value()}

    decision = policy.evaluate_pipeline_completion(pipeline)

    assert decision.reason_code == "preparation_invalid"


def test_pipeline_state_hash_translates_deep_json_to_value_error() -> None:
    policy = _policy()
    pipeline = _pipeline()
    _step(pipeline, "生成素材任务").output_summary = {"deep": _deep_json_value()}

    with pytest.raises(ValueError, match="strict JSON"):
        policy.pipeline_state_sha256(pipeline)


@pytest.mark.parametrize("relationship_problem", ["missing", "wrong_id", "wrong_topic"])
def test_preparation_rejects_missing_or_mismatched_current_script(
    relationship_problem: str,
) -> None:
    policy = _policy()
    pipeline = _pipeline()
    if relationship_problem == "missing":
        pipeline.script_asset = None
    elif relationship_problem == "wrong_id":
        pipeline.script_asset = _script(script_id=99)
    else:
        pipeline.script_asset = _script(topic_idea_id=99)

    decision = policy.evaluate_pipeline_completion(pipeline)

    assert decision.reason_code == "preparation_invalid"


def test_step_output_id_reads_only_positive_plain_integer_ids() -> None:
    policy = _policy()

    assert policy.step_output_id(SimpleNamespace(output_summary={"topic_idea_id": 12})) == 12
    assert policy.step_output_id(SimpleNamespace(output_summary={"script_asset_id": 13})) == 13
    assert policy.step_output_id(SimpleNamespace(output_summary={"topic_idea_id": True})) is None
    assert policy.step_output_id(SimpleNamespace(output_summary={"topic_idea_id": 0})) is None
    assert policy.step_output_id(SimpleNamespace(output_summary=[])) is None


def test_pipeline_state_hash_is_canonical_and_binds_current_mutable_state() -> None:
    policy = _policy()
    baseline = _pipeline()
    baseline_hash = policy.pipeline_state_sha256(baseline)

    reordered = copy.deepcopy(baseline)
    _step(reordered, "生成脚本素材").output_summary = {
        "from_cache": False,
        "script_asset_id": 20,
    }
    assert policy.pipeline_state_sha256(reordered) == baseline_hash

    for mutation in ("hotspot", "output_id", "summary", "script"):
        changed = copy.deepcopy(baseline)
        if mutation == "hotspot":
            changed.hotspot_id += 1
        elif mutation == "output_id":
            _step(changed, "选择候选选题").output_summary["topic_idea_id"] += 1
        elif mutation == "summary":
            _step(changed, "生成素材任务").output_summary["new_value"] = "changed"
        else:
            changed.script_asset.voiceover = "上游脚本已经改变。"
        assert policy.pipeline_state_sha256(changed) != baseline_hash

    ignored = copy.deepcopy(baseline)
    ignored.status = "已完成"
    ignored.completion_evidence = {"anything": "ignored"}
    ignored.updated_at = datetime(2030, 1, 1, tzinfo=timezone.utc)
    assert policy.pipeline_state_sha256(ignored) == baseline_hash


def test_decision_is_immutable_and_model_relationship_is_read_only() -> None:
    policy = _policy()
    decision = policy.evaluate_pipeline_completion(_pipeline())

    with pytest.raises(FrozenInstanceError):
        decision.status = "已完成"
    assert ProductionPipeline.script_asset.property.viewonly is True
    evidence_column = ProductionPipeline.__table__.columns["completion_evidence"]
    assert evidence_column.nullable is False


def test_service_serialization_reuses_canonical_step_output_id(monkeypatch) -> None:
    _policy()
    service = importlib.import_module("app.pipeline.service")
    pipeline = _pipeline()
    monkeypatch.setattr(service, "step_output_id", lambda _step: 987)

    result = service._build_result(pipeline)

    assert result.steps
    assert {item.output_id for item in result.steps} == {987}


def test_valid_approval_verifies_guarded_artifact_and_all_five_gates(
    tmp_path: Path,
) -> None:
    policy = _policy()
    pipeline, _artifact_path, _report_path, _report = _approved_project(tmp_path)

    decision = policy.evaluate_pipeline_completion(
        pipeline,
        now=NOW,
        system_drive="C:",
    )

    assert decision.is_complete is True
    assert decision.status == "已完成"
    assert decision.reason_code == "approved_artifact_verified"
    assert decision.current_step == "已完成"
    assert decision.next_surface == "none"
    assert decision.needs_confirmation is False


@pytest.mark.parametrize(
    "mutation",
    ["missing", "extra", "wrong_version", "non_dict", "non_json"],
)
def test_evidence_envelope_is_exact_strict_json(mutation: str, tmp_path: Path) -> None:
    policy = _policy()
    pipeline, _artifact_path, _report_path, _report = _approved_project(tmp_path)
    if mutation == "missing":
        pipeline.completion_evidence.pop("artifact_sha256")
    elif mutation == "extra":
        pipeline.completion_evidence["absolute_path"] = "D:/secret/final.mp4"
    elif mutation == "wrong_version":
        pipeline.completion_evidence["evidence_version"] = True
    elif mutation == "non_dict":
        pipeline.completion_evidence = []
    else:
        pipeline.completion_evidence["artifact_sha256"] = {"not-json"}

    decision = policy.evaluate_pipeline_completion(pipeline, now=NOW, system_drive="C:")

    assert decision.reason_code == "evidence_invalid"
    assert decision.next_surface == "finished"


def test_deep_completion_evidence_fails_closed_without_recursion_error(
    tmp_path: Path,
) -> None:
    policy = _policy()
    pipeline, _artifact_path, _report_path, _report = _approved_project(tmp_path)
    pipeline.completion_evidence["approved_by"] = _deep_json_value()

    decision = policy.evaluate_pipeline_completion(pipeline, now=NOW, system_drive="C:")

    assert decision.reason_code == "evidence_invalid"


@pytest.mark.parametrize(
    ("approved_by", "approved_at"),
    [
        ("ian", APPROVED_AT.isoformat()),
        ("Ian ", APPROVED_AT.isoformat()),
        ("Ian", APPROVED_AT.replace(tzinfo=None).isoformat()),
        ("Ian", (NOW + timedelta(minutes=6)).isoformat()),
    ],
)
def test_approval_identity_and_time_fail_closed(
    approved_by: str,
    approved_at: str,
    tmp_path: Path,
) -> None:
    policy = _policy()
    pipeline, _artifact_path, _report_path, _report = _approved_project(tmp_path)
    pipeline.completion_evidence["approved_by"] = approved_by
    pipeline.completion_evidence["approved_at"] = approved_at

    decision = policy.evaluate_pipeline_completion(pipeline, now=NOW, system_drive="C:")

    assert decision.reason_code == "approval_invalid"


def test_approval_binds_exact_current_pipeline_state(tmp_path: Path) -> None:
    policy = _policy()
    pipeline, _artifact_path, _report_path, _report = _approved_project(tmp_path)
    pipeline.completion_evidence["pipeline_state_sha256"] = "0" * 64

    decision = policy.evaluate_pipeline_completion(pipeline, now=NOW, system_drive="C:")

    assert decision.reason_code == "pipeline_state_mismatch"


@pytest.mark.parametrize(
    "raw_path",
    [
        "",
        ".",
        "成片/.",
        "成片/../final.mp4",
        "成片//final.mp4",
        "成片/final.mp4/",
        "成片\\final.mp4",
        "成片/sub\\final.mp4",
        "素材/final.mp4",
        "/成片/final.mp4",
        "C:/成片/final.mp4",
        "C:\\成片\\final.mp4",
        "\\\\server\\share\\final.mp4",
        "file:///成片/final.mp4",
        "https://example.test/final.mp4",
        "成片/final.mov",
    ],
)
def test_artifact_raw_path_must_be_canonical_under_finished(
    raw_path: str,
    tmp_path: Path,
) -> None:
    policy = _policy()
    pipeline, _artifact_path, _report_path, _report = _approved_project(tmp_path)
    pipeline.completion_evidence["artifact_relative_path"] = raw_path

    decision = policy.evaluate_pipeline_completion(pipeline, now=NOW, system_drive="C:")

    assert decision.reason_code == "artifact_path_invalid"


@pytest.mark.parametrize(
    "raw_path",
    [
        "成片/report.txt",
        "成片//quality-report.json",
        "成片\\quality-report.json",
        "../quality-report.json",
        "D:/quality-report.json",
        "file:///quality-report.json",
    ],
)
def test_report_raw_path_must_be_canonical_under_finished(
    raw_path: str,
    tmp_path: Path,
) -> None:
    policy = _policy()
    pipeline, _artifact_path, _report_path, _report = _approved_project(tmp_path)
    pipeline.completion_evidence["quality_report_relative_path"] = raw_path

    decision = policy.evaluate_pipeline_completion(pipeline, now=NOW, system_drive="C:")

    assert decision.reason_code == "quality_report_path_invalid"


def test_missing_artifact_is_not_completion(tmp_path: Path) -> None:
    policy = _policy()
    pipeline, artifact_path, _report_path, _report = _approved_project(tmp_path)
    artifact_path.unlink()

    decision = policy.evaluate_pipeline_completion(pipeline, now=NOW, system_drive="C:")

    assert decision.reason_code == "artifact_missing"


def test_invalid_project_identity_is_rejected_before_any_leaf_probe(
    tmp_path: Path,
    monkeypatch,
) -> None:
    policy = _policy()
    identity = importlib.import_module("app.pipeline.project_identity")
    pipeline, _artifact_path, _report_path, _report = _approved_project(tmp_path)

    def reject_project_use(*_args, **_kwargs):
        raise identity.ProjectIdentityError("invalid project authority")

    monkeypatch.setattr(policy, "begin_prepared_project_directory_use", reject_project_use)
    monkeypatch.setattr(
        policy.os.path,
        "lexists",
        lambda _path: pytest.fail("leaf probed before project identity validation"),
    )

    decision = policy.evaluate_pipeline_completion(pipeline, now=NOW, system_drive="C:")

    assert decision.reason_code == "artifact_unsafe"


def test_rejected_child_guard_prevents_leaf_probe(
    tmp_path: Path,
    monkeypatch,
) -> None:
    policy = _policy()
    identity = importlib.import_module("app.pipeline.project_identity")
    pipeline, artifact_path, _report_path, _report = _approved_project(tmp_path)
    original_lexists = os.path.lexists

    def reject_child_use(*_args, **_kwargs):
        raise identity.ProjectIdentityError("child guard rejected")

    def fail_on_artifact_probe(path):
        if Path(path) == artifact_path:
            pytest.fail("artifact leaf probed before child authorization")
        return original_lexists(path)

    monkeypatch.setattr(policy, "begin_prepared_project_child_directory_use", reject_child_use)
    monkeypatch.setattr(policy.os.path, "lexists", fail_on_artifact_probe)

    decision = policy.evaluate_pipeline_completion(pipeline, now=NOW, system_drive="C:")

    assert decision.reason_code == "artifact_unsafe"


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"this is not a video",
        _box(b"moov") + _box(b"mdat", b"x"),
        _box(b"ftyp") + _box(b"mdat", b"x"),
        _box(b"ftyp") + _box(b"moov"),
        struct.pack(">I4s", 4, b"ftyp"),
        struct.pack(">I4s", 9999, b"ftyp") + b"tiny",
        struct.pack(">I4s", 0, b"ftyp") + _box(b"moov") + _box(b"mdat"),
    ],
)
def test_artifact_must_be_bounded_iso_bmff_candidate(
    content: bytes,
    tmp_path: Path,
) -> None:
    policy = _policy()
    pipeline, artifact_path, report_path, report = _approved_project(tmp_path)
    _rewrite_artifact_and_bind_report(
        pipeline,
        artifact_path,
        report_path,
        report,
        content,
    )

    decision = policy.evaluate_pipeline_completion(pipeline, now=NOW, system_drive="C:")

    assert decision.reason_code == "artifact_format_invalid"


@pytest.mark.parametrize(
    "content",
    [
        _box(b"ftyp") + _box(b"moov") + _box(b"mdat"),
        _box(b"ftyp", b"isom")
        + _box(b"moov", _box(b"mvhd"))
        + _box(b"mdat", b"x"),
        _box(b"ftyp", b"isom\x00\x00\x02\x00")
        + _box(b"moov", b"not-a-box")
        + _box(b"mdat", b"x"),
        _box(b"ftyp", b"isom\x00\x00\x02\x00")
        + _box(b"moov", _box(b"mvhd"))
        + _box(b"mdat"),
    ],
)
def test_iso_bmff_required_boxes_need_minimum_credible_payloads(
    content: bytes,
    tmp_path: Path,
) -> None:
    policy = _policy()
    pipeline, artifact_path, report_path, report = _approved_project(tmp_path)
    _rewrite_artifact_and_bind_report(
        pipeline,
        artifact_path,
        report_path,
        report,
        content,
    )

    decision = policy.evaluate_pipeline_completion(pipeline, now=NOW, system_drive="C:")

    assert decision.reason_code == "artifact_format_invalid"


def test_artifact_hash_mismatch_fails_before_report(tmp_path: Path) -> None:
    policy = _policy()
    pipeline, _artifact_path, _report_path, _report = _approved_project(tmp_path)
    pipeline.completion_evidence["artifact_sha256"] = "f" * 64

    decision = policy.evaluate_pipeline_completion(pipeline, now=NOW, system_drive="C:")

    assert decision.reason_code == "artifact_hash_mismatch"


@pytest.mark.parametrize("leaf", ["artifact", "report"])
def test_hard_link_artifact_and_report_are_unsafe(leaf: str, tmp_path: Path) -> None:
    policy = _policy()
    pipeline, artifact_path, report_path, _report = _approved_project(tmp_path)
    target = artifact_path if leaf == "artifact" else report_path
    outside = tmp_path / f"outside-{leaf}.bin"
    outside.write_bytes(target.read_bytes())
    target.unlink()
    os.link(outside, target)

    decision = policy.evaluate_pipeline_completion(pipeline, now=NOW, system_drive="C:")

    assert decision.reason_code == (
        "artifact_unsafe" if leaf == "artifact" else "quality_report_unsafe"
    )


@pytest.mark.parametrize("leaf", ["artifact", "report"])
def test_reparse_leaf_is_rejected_without_privileged_link_creation(
    leaf: str,
    tmp_path: Path,
    monkeypatch,
) -> None:
    policy = _policy()
    identity = importlib.import_module("app.pipeline.project_identity")
    pipeline, artifact_path, report_path, _report = _approved_project(tmp_path)
    target = artifact_path if leaf == "artifact" else report_path
    original = identity._is_reparse
    monkeypatch.setattr(
        identity,
        "_is_reparse",
        lambda path: Path(path) == target or original(path),
    )

    decision = policy.evaluate_pipeline_completion(pipeline, now=NOW, system_drive="C:")

    assert decision.reason_code == (
        "artifact_unsafe" if leaf == "artifact" else "quality_report_unsafe"
    )


def test_reparse_finished_directory_is_unsafe_without_junction_privilege(
    tmp_path: Path,
    monkeypatch,
) -> None:
    policy = _policy()
    identity = importlib.import_module("app.pipeline.project_identity")
    pipeline, artifact_path, _report_path, _report = _approved_project(tmp_path)
    finished = artifact_path.parent
    original = identity._is_reparse
    monkeypatch.setattr(
        identity,
        "_is_reparse",
        lambda path: Path(path) == finished or original(path),
    )

    decision = policy.evaluate_pipeline_completion(pipeline, now=NOW, system_drive="C:")

    assert decision.reason_code == "artifact_unsafe"


@pytest.mark.parametrize("attack", ["in_place", "exchange"])
def test_artifact_change_during_handle_bound_read_is_unsafe(
    attack: str,
    tmp_path: Path,
    monkeypatch,
) -> None:
    policy = _policy()
    pipeline, artifact_path, _report_path, _report = _approved_project(tmp_path)
    original = policy._stream_sha256
    attacked = False

    def attack_after_read(stream):
        nonlocal attacked
        digest = original(stream)
        if not attacked:
            attacked = True
            replacement = _valid_mp4().replace(b"verified", b"tampered")
            if attack == "in_place":
                artifact_path.write_bytes(replacement)
            else:
                temporary = artifact_path.with_suffix(".replacement")
                temporary.write_bytes(replacement)
                os.replace(temporary, artifact_path)
        return digest

    monkeypatch.setattr(policy, "_stream_sha256", attack_after_read)

    decision = policy.evaluate_pipeline_completion(pipeline, now=NOW, system_drive="C:")

    assert decision.reason_code == "artifact_unsafe"


def test_missing_report_is_not_completion(tmp_path: Path) -> None:
    policy = _policy()
    pipeline, _artifact_path, report_path, _report = _approved_project(tmp_path)
    report_path.unlink()

    decision = policy.evaluate_pipeline_completion(pipeline, now=NOW, system_drive="C:")

    assert decision.reason_code == "quality_report_missing"


def test_report_larger_than_two_mib_is_rejected(tmp_path: Path) -> None:
    policy = _policy()
    pipeline, _artifact_path, report_path, _report = _approved_project(tmp_path)
    content = b"{" + b" " * (2 * 1024 * 1024) + b"}"
    report_path.write_bytes(content)
    pipeline.completion_evidence["quality_report_sha256"] = _sha256(content)

    decision = policy.evaluate_pipeline_completion(pipeline, now=NOW, system_drive="C:")

    assert decision.reason_code == "quality_report_too_large"


@pytest.mark.parametrize(
    "content",
    [
        b"\xef\xbb\xbf{}",
        b"{not-json}",
        b'{"report_format":"x","report_format":"y"}',
        b'{"score":NaN}',
        b'{"score":Infinity}',
        b'{"score":-Infinity}',
    ],
)
def test_report_json_is_strict(content: bytes, tmp_path: Path) -> None:
    policy = _policy()
    pipeline, _artifact_path, report_path, _report = _approved_project(tmp_path)
    report_path.write_bytes(content)
    pipeline.completion_evidence["quality_report_sha256"] = _sha256(content)

    decision = policy.evaluate_pipeline_completion(pipeline, now=NOW, system_drive="C:")

    assert decision.reason_code == "quality_report_invalid"


def test_deep_report_under_size_limit_fails_closed_without_recursion_error(
    tmp_path: Path,
) -> None:
    policy = _policy()
    pipeline, _artifact_path, report_path, _report = _approved_project(tmp_path)
    content = b'{"deep":' + b"[" * 500 + b"0" + b"]" * 500 + b"}"
    assert len(content) < 2 * 1024 * 1024
    assert isinstance(json.loads(content), dict)
    report_path.write_bytes(content)
    pipeline.completion_evidence["quality_report_sha256"] = _sha256(content)

    decision = policy.evaluate_pipeline_completion(pipeline, now=NOW, system_drive="C:")

    assert decision.reason_code == "quality_report_invalid"


def test_report_hash_mismatch_is_rejected(tmp_path: Path) -> None:
    policy = _policy()
    pipeline, _artifact_path, _report_path, _report = _approved_project(tmp_path)
    pipeline.completion_evidence["quality_report_sha256"] = "e" * 64

    decision = policy.evaluate_pipeline_completion(pipeline, now=NOW, system_drive="C:")

    assert decision.reason_code == "quality_report_hash_mismatch"


def test_v2_completion_rechecks_immutable_quality_contract_on_every_read(
    tmp_path: Path,
) -> None:
    policy = _policy()
    pipeline, _artifact_path, report_path, report = _approved_project(tmp_path)
    contract_path = _bind_quality_contract_v2(pipeline, report_path, report)

    assert policy.evaluate_pipeline_completion(
        pipeline,
        now=NOW,
        system_drive="C:",
    ).is_complete is True

    contract_path.write_text('{"tampered":true}\n', encoding="utf-8")
    decision = policy.evaluate_pipeline_completion(
        pipeline,
        now=NOW,
        system_drive="C:",
    )

    assert decision.is_complete is False
    assert decision.reason_code == "quality_contract_hash_mismatch"


def test_v2_completion_rechecks_every_frozen_reference_hash(tmp_path: Path) -> None:
    policy = _policy()
    pipeline, _artifact_path, report_path, report = _approved_project(tmp_path)
    contract_path = _bind_quality_contract_v2(pipeline, report_path, report)
    reference = Path(pipeline.project_dir) / "质量合同" / "references" / "ref.mp4"
    reference.parent.mkdir(parents=True)
    reference.write_bytes(b"frozen-reference")
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract["reference_pack"] = {
        "status": "frozen",
        "items": [
            {
                "id": "ref_1",
                "project_relative_path": "质量合同/references/ref.mp4",
                "sha256": _sha256(reference.read_bytes()),
            }
        ],
    }
    _rewrite_contract_and_binding(
        pipeline,
        contract_path,
        report_path,
        report,
        contract,
    )

    assert policy.evaluate_pipeline_completion(
        pipeline,
        now=NOW,
        system_drive="C:",
    ).is_complete is True

    reference.write_bytes(b"replaced-reference")
    decision = policy.evaluate_pipeline_completion(
        pipeline,
        now=NOW,
        system_drive="C:",
    )

    assert decision.is_complete is False
    assert decision.reason_code == "reference_hash_mismatch"


def test_v2_completion_fails_closed_when_quality_contract_is_missing(
    tmp_path: Path,
) -> None:
    policy = _policy()
    pipeline, _artifact_path, report_path, report = _approved_project(tmp_path)
    contract_path = _bind_quality_contract_v2(pipeline, report_path, report)
    contract_path.unlink()

    decision = policy.evaluate_pipeline_completion(
        pipeline,
        now=NOW,
        system_drive="C:",
    )

    assert decision.is_complete is False
    assert decision.reason_code == "quality_contract_missing"


def test_v2_completion_rejects_checkbox_only_quality_contract_evidence(
    tmp_path: Path,
) -> None:
    policy = _policy()
    pipeline, _artifact_path, _report_path, _report = _approved_project(tmp_path)
    pipeline.completion_evidence["evidence_version"] = 2

    decision = policy.evaluate_pipeline_completion(
        pipeline,
        now=NOW,
        system_drive="C:",
    )

    assert decision.is_complete is False
    assert decision.reason_code == "quality_contract_review_invalid"


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_top",
        "extra_top",
        "missing_gate",
        "extra_gate",
        "extra_gate_field",
        "unknown_status",
        "non_list_evidence",
        "non_string_evidence",
        "blank_evidence",
    ],
)
def test_quality_report_contract_is_exact(
    mutation: str,
    tmp_path: Path,
) -> None:
    policy = _policy()
    pipeline, _artifact_path, report_path, report = _approved_project(tmp_path)
    report = copy.deepcopy(report)
    if mutation == "missing_top":
        report.pop("report_format")
    elif mutation == "extra_top":
        report["notes"] = []
    elif mutation == "missing_gate":
        report["gates"].pop("technical")
    elif mutation == "extra_gate":
        report["gates"]["vibes"] = {"status": "passed", "evidence": ["ok"]}
    elif mutation == "extra_gate_field":
        report["gates"]["technical"]["score"] = 100
    elif mutation == "unknown_status":
        report["gates"]["technical"]["status"] = "warning"
    elif mutation == "non_list_evidence":
        report["gates"]["technical"]["evidence"] = "ok"
    elif mutation == "non_string_evidence":
        report["gates"]["technical"]["evidence"] = [1]
    else:
        report["gates"]["technical"]["evidence"] = ["  "]
    _rewrite_report(pipeline, report_path, report)

    decision = policy.evaluate_pipeline_completion(pipeline, now=NOW, system_drive="C:")

    assert decision.reason_code == "quality_report_invalid"


@pytest.mark.parametrize(
    "gate",
    [
        "structure",
        "technical",
        "visual_narration",
        "facts_rights",
        "quality_contract",
    ],
)
def test_each_structurally_valid_failed_gate_blocks_release(
    gate: str,
    tmp_path: Path,
) -> None:
    policy = _policy()
    pipeline, _artifact_path, report_path, report = _approved_project(tmp_path)
    report["gates"][gate] = {"status": "failed", "evidence": ["发现阻断问题"]}
    _rewrite_report(pipeline, report_path, report)

    decision = policy.evaluate_pipeline_completion(pipeline, now=NOW, system_drive="C:")

    assert decision.reason_code == "quality_gate_failed"


def test_non_empty_trimmed_release_blocker_blocks_release(tmp_path: Path) -> None:
    policy = _policy()
    pipeline, _artifact_path, report_path, report = _approved_project(tmp_path)
    report["release_blockers"] = ["字幕仍有错字"]
    _rewrite_report(pipeline, report_path, report)

    decision = policy.evaluate_pipeline_completion(pipeline, now=NOW, system_drive="C:")

    assert decision.reason_code == "quality_gate_failed"


@pytest.mark.parametrize("binding", ["pipeline", "state", "artifact"])
def test_report_must_bind_pipeline_state_and_artifact(
    binding: str,
    tmp_path: Path,
) -> None:
    policy = _policy()
    pipeline, _artifact_path, report_path, report = _approved_project(tmp_path)
    if binding == "pipeline":
        report["pipeline_id"] += 1
    elif binding == "state":
        report["pipeline_state_sha256"] = "1" * 64
    else:
        report["artifact_sha256"] = "2" * 64
    _rewrite_report(pipeline, report_path, report)

    decision = policy.evaluate_pipeline_completion(pipeline, now=NOW, system_drive="C:")

    assert decision.reason_code == "quality_report_mismatch"


@pytest.mark.parametrize("time_problem", ["naive", "future", "after_approval"])
def test_report_generation_time_is_timezone_aware_and_ordered(
    time_problem: str,
    tmp_path: Path,
) -> None:
    policy = _policy()
    pipeline, _artifact_path, report_path, report = _approved_project(tmp_path)
    if time_problem == "naive":
        report["generated_at"] = APPROVED_AT.replace(tzinfo=None).isoformat()
    elif time_problem == "future":
        report["generated_at"] = (NOW + timedelta(minutes=6)).isoformat()
    else:
        report["generated_at"] = (APPROVED_AT + timedelta(seconds=1)).isoformat()
    _rewrite_report(pipeline, report_path, report)

    decision = policy.evaluate_pipeline_completion(pipeline, now=NOW, system_drive="C:")

    assert decision.reason_code == "quality_report_invalid"


@pytest.mark.parametrize("mutation", ["hotspot", "output_id", "summary", "script"])
def test_any_approved_upstream_state_change_invalidates_completion(
    mutation: str,
    tmp_path: Path,
) -> None:
    policy = _policy()
    pipeline, _artifact_path, _report_path, _report = _approved_project(tmp_path)
    if mutation == "hotspot":
        pipeline.hotspot_id += 1
    elif mutation == "output_id":
        _step(pipeline, "选择候选选题").output_summary["topic_idea_id"] = 11
        pipeline.topic_idea_id = 11
        pipeline.script_asset.topic_idea_id = 11
    elif mutation == "summary":
        _step(pipeline, "生成素材任务").output_summary["changed"] = True
    else:
        pipeline.script_asset.voiceover = "批准后口播稿被修改"

    decision = policy.evaluate_pipeline_completion(pipeline, now=NOW, system_drive="C:")

    assert decision.reason_code == "pipeline_state_mismatch"


def test_real_rewrite_script_part_invalidates_prior_approval(
    tmp_path: Path,
    monkeypatch,
) -> None:
    policy = _policy()
    rewrite = importlib.import_module("app.pipeline.rewrite_service")
    pipeline, _artifact_path, _report_path, _report = _approved_project(tmp_path)
    script = pipeline.script_asset

    class FakeSession:
        def get(self, model, object_id):
            if model.__name__ == "ProductionPipeline" and object_id == pipeline.id:
                return pipeline
            if model.__name__ == "ScriptAsset" and object_id == script.id:
                return script
            return None

        def add(self, _value):
            return None

        def commit(self):
            return None

        def refresh(self, _value):
            return None

    monkeypatch.setattr(rewrite, "get_account_style_context", lambda _session: {})
    monkeypatch.setattr(rewrite, "get_script_engine_context", lambda _session: {"samples": []})
    rewrite.rewrite_script_part(FakeSession(), pipeline.id, "voiceover")

    decision = policy.evaluate_pipeline_completion(pipeline, now=NOW, system_drive="C:")

    assert decision.reason_code == "pipeline_state_mismatch"
