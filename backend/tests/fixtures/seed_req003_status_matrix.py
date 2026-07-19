from __future__ import annotations

import hashlib
import json
import os
import struct
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core.database import SessionLocal  # noqa: E402
from app.models import PipelineStep, ProductionPipeline  # noqa: E402
from app.pipeline.completion_policy import (  # noqa: E402
    evaluate_pipeline_completion,
    pipeline_state_sha256,
)
from app.pipeline.models import PIPELINE_STEP_NAMES  # noqa: E402
from app.pipeline.project_identity import (  # noqa: E402
    add_pipeline_with_identity,
    begin_prepared_project_child_directory_use,
    prepare_pipeline_project_directory,
)


PROJECT_NAMES = {
    "running": "REQ003-运行中项目",
    "failed": "REQ003-失败阻断项目",
    "approval": "REQ003-待成片批准项目",
    "stale": "REQ003-批准后成片变更项目",
}


def _box(kind: bytes, payload: bytes = b"") -> bytes:
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def _valid_mp4(frame_payload: bytes) -> bytes:
    return (
        _box(b"ftyp", b"isom\x00\x00\x02\x00isom")
        + _box(b"moov", _box(b"mvhd"))
        + _box(b"mdat", frame_payload)
    )


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _json_bytes(payload: dict) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _step_statuses(kind: str) -> list[str]:
    if kind == "running":
        return ["已完成", "已完成", "已完成", "进行中"] + ["未开始"] * (
            len(PIPELINE_STEP_NAMES) - 4
        )
    if kind == "failed":
        return ["已完成", "已完成", "已完成", "失败"] + ["未开始"] * (
            len(PIPELINE_STEP_NAMES) - 4
        )
    return ["已完成"] * len(PIPELINE_STEP_NAMES)


def _add_pipeline(session, base: ProductionPipeline, kind: str, project_root: Path):
    pipeline = ProductionPipeline(
        hotspot_id=base.hotspot_id,
        topic_idea_id=base.topic_idea_id,
        script_asset_id=base.script_asset_id,
        project_name=PROJECT_NAMES[kind],
        project_dir="",
        status="已完成" if kind != "failed" else "进行中",
        provider=base.provider,
        model=base.model,
    )
    add_pipeline_with_identity(session, pipeline)
    statuses = _step_statuses(kind)
    for index, (name, status) in enumerate(
        zip(PIPELINE_STEP_NAMES, statuses, strict=True),
        start=1,
    ):
        summary: dict = {}
        if name == "选择候选选题" and status == "已完成":
            summary = {"topic_idea_id": base.topic_idea_id}
        elif name == "生成脚本素材" and status == "已完成":
            summary = {"script_asset_id": base.script_asset_id, "from_cache": False}
        pipeline.steps.append(
            PipelineStep(
                name=name,
                step_order=index,
                status=status,
                error="REQ-003 注入的素材步骤失败。" if status == "失败" else "",
                output_summary=summary,
            )
        )
    session.flush()
    prepare_pipeline_project_directory(
        pipeline,
        project_root=project_root,
        system_drive="C:",
    )
    session.flush()
    return pipeline


def _bind_then_change_approved_artifact(
    pipeline: ProductionPipeline,
    project_root: Path,
) -> None:
    approved_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    finished = begin_prepared_project_child_directory_use(
        pipeline,
        project_root=project_root,
        relative_path="成片",
        create=True,
        system_drive="C:",
    )
    artifact_path = finished.path / "req003-approved.mp4"
    report_path = finished.path / "req003-quality-report.json"
    approved_artifact = _valid_mp4(b"approved-frame")
    artifact_path.write_bytes(approved_artifact)
    state_hash = pipeline_state_sha256(pipeline)
    report = {
        "report_format": "auto-video-engine-quality-report-v1",
        "pipeline_id": pipeline.id,
        "pipeline_state_sha256": state_hash,
        "artifact_sha256": _sha256(approved_artifact),
        "generated_at": (approved_at - timedelta(seconds=1)).isoformat(),
        "gates": {
            gate: {"status": "passed", "evidence": [f"REQ-003 {gate} 已检查"]}
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
        "artifact_relative_path": "成片/req003-approved.mp4",
        "artifact_sha256": _sha256(approved_artifact),
        "quality_report_relative_path": "成片/req003-quality-report.json",
        "quality_report_sha256": _sha256(report_bytes),
        "approved_at": approved_at.isoformat(),
        "approved_by": "Ian",
    }

    # Keep the file structurally valid while changing its bytes.  The resulting
    # state is a real once-approved artifact whose approval has gone stale.
    artifact_path.write_bytes(_valid_mp4(b"changed-after-approval"))


def main() -> None:
    project_root_raw = os.environ.get("PROJECT_ROOT_DIR", "")
    project_root = Path(project_root_raw)
    if not project_root.is_absolute() or project_root.drive.upper() != "D:":
        raise RuntimeError("REQ-003 fixture requires an absolute D: project root")
    project_root.mkdir(parents=True, exist_ok=True)

    with SessionLocal() as session:
        existing = {
            item.project_name: item
            for item in session.query(ProductionPipeline)
            .filter(ProductionPipeline.project_name.in_(PROJECT_NAMES.values()))
            .all()
        }
        if existing and set(existing) != set(PROJECT_NAMES.values()):
            raise RuntimeError("REQ-003 fixture is partially present; refusing to guess")

        if not existing:
            base = (
                session.query(ProductionPipeline)
                .filter(ProductionPipeline.topic_idea_id.is_not(None))
                .filter(ProductionPipeline.script_asset_id.is_not(None))
                .order_by(ProductionPipeline.id.asc())
                .first()
            )
            if base is None:
                raise RuntimeError("REQ-003 fixture needs the completed golden project first")
            created = {
                kind: _add_pipeline(session, base, kind, project_root)
                for kind in PROJECT_NAMES
            }
            _bind_then_change_approved_artifact(created["stale"], project_root)
            session.commit()

        rows = {
            item.project_name: item
            for item in session.query(ProductionPipeline)
            .filter(ProductionPipeline.project_name.in_(PROJECT_NAMES.values()))
            .all()
        }
        expected = {
            "running": "step_running",
            "failed": "step_failed",
            "approval": "approval_missing",
            "stale": "artifact_hash_mismatch",
        }
        result: dict[str, dict[str, object]] = {}
        for kind, name in PROJECT_NAMES.items():
            pipeline = rows[name]
            decision = evaluate_pipeline_completion(
                pipeline,
                system_drive="C:",
            )
            if decision.reason_code != expected[kind]:
                raise RuntimeError(
                    f"REQ-003 {kind} expected {expected[kind]}, got {decision.reason_code}"
                )
            result[kind] = {
                "pipeline_id": pipeline.id,
                "reason_code": decision.reason_code,
            }

    # Keep the runner log ASCII-only so Windows PowerShell cannot corrupt the
    # evidence when its active console code page is not UTF-8.
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
