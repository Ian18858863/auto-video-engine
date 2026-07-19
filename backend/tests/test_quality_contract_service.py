from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.database import Base
from app.models import HotItem, ProductionPipeline
from app.pipeline.quality_contract_service import (
    QualityContractError,
    ensure_default_quality_contract,
    evaluate_quality_contract_review,
    get_quality_contract,
)
from app.pipeline.service import build_project_from_hotspot


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return Session(engine)


def _build_pipeline(session: Session, root: Path) -> tuple[int, Path]:
    (root / "项目").mkdir(parents=True, exist_ok=True)
    hotspot = HotItem(
        source_platform="test",
        source_type="manual",
        title="quality contract",
        url="https://example.test/quality-contract",
        published_at=datetime.now(timezone.utc),
        summary="quality contract",
        tags=["test"],
        category="test",
        hot_score=90,
        topic_score=90,
        save_score=90,
    )
    session.add(hotspot)
    session.commit()
    session.refresh(hotspot)
    built = build_project_from_hotspot(session, hotspot.id, material_root=root)
    return built.pipeline_id, root / "项目"


def _structured_review(snapshot) -> dict[str, object]:
    return {
        "contract_sha256": snapshot.sha256,
        "must_preserve_results": {
            item["id"]: {
                "result": "passed",
                "evidence": f"已复核：{item['criterion']}",
            }
            for item in snapshot.contract["must_preserve"]
        },
        "allowed_difference_results": {
            item["id"]: {
                "result": "not_used",
                "actual_difference": "",
                "evidence": "本次没有使用这项允许差异",
            }
            for item in snapshot.contract["allowed_differences"]
        },
        "planned_exceed_results": {
            item["id"]: {
                "result": "exceeded",
                "difference": f"已达到并超越：{item['criterion']}",
                "evidence": f"逐项比较：{item['label']}",
            }
            for item in snapshot.contract["planned_exceed_dimensions"]
        },
    }


def test_default_quality_contract_is_project_local_immutable_and_complete(
    tmp_path: Path,
):
    root = tmp_path / "D" / "factory"
    with _session() as session:
        pipeline_id, project_root = _build_pipeline(session, root)
        pipeline = session.get(ProductionPipeline, pipeline_id)
        assert pipeline is not None

        snapshot = get_quality_contract(
            session,
            pipeline_id,
            project_root=project_root,
        )
        assert ensure_default_quality_contract(
            session,
            pipeline_id,
            project_root=project_root,
        ).sha256 == snapshot.sha256
        assert snapshot.relative_path == "质量合同/quality-contract-v1.json"
        assert len(snapshot.sha256) == 64
        assert str(pipeline.project_dir) not in repr(snapshot.to_dict())
        assert snapshot.contract["target"] == {
            "platform": "抖音",
            "aspect_ratio": "9:16",
            "duration_seconds": 60,
        }
        assert snapshot.contract["reference_pack"] == {
            "status": "none_supplied",
            "items": [],
        }
        assert snapshot.contract["must_preserve"]
        assert snapshot.contract["allowed_differences"]
        assert snapshot.contract["planned_exceed_dimensions"]
        assert all(item["criterion"] for item in snapshot.contract["must_preserve"])
        assert all(
            item["required_result"] == "exceeded"
            for item in snapshot.contract["planned_exceed_dimensions"]
        )

        repeated = ensure_default_quality_contract(
            session,
            pipeline_id,
            project_root=project_root,
        )
        assert repeated.sha256 == snapshot.sha256
        assert repeated.contract == snapshot.contract
        assert get_quality_contract(
            session,
            pipeline_id,
            project_root=project_root,
        ).sha256 == snapshot.sha256


def test_quality_contract_review_requires_hash_every_item_and_records_differences(
    tmp_path: Path,
):
    root = tmp_path / "D" / "factory"
    with _session() as session:
        pipeline_id, project_root = _build_pipeline(session, root)
        snapshot = ensure_default_quality_contract(
            session,
            pipeline_id,
            project_root=project_root,
        )

        missing = evaluate_quality_contract_review(
            session,
            pipeline_id,
            None,
            project_root=project_root,
        )
        assert missing == {
            "status": "failed",
            "evidence": ["quality_contract_review_required"],
        }

        review = _structured_review(snapshot)
        allowed_id = snapshot.contract["allowed_differences"][0]["id"]
        exceed_id = snapshot.contract["planned_exceed_dimensions"][0]["id"]
        review["allowed_difference_results"][allowed_id] = {
            "result": "used",
            "actual_difference": "旁白实测比计划长 2.1 秒，镜头按音频重新定时",
            "evidence": "TimelineSpec audio_first_timing 版本记录",
        }
        gate = evaluate_quality_contract_review(
            session,
            pipeline_id,
            review,
            project_root=project_root,
        )
        assert gate["status"] == "passed"
        assert f"quality_contract_sha256:{snapshot.sha256}" in gate["evidence"]
        assert any(
            item.startswith("allowed_difference_review:")
            and f'"id":"{allowed_id}"' in item
            and '"result":"used"' in item
            for item in gate["evidence"]
        )
        assert any(
            item.startswith("planned_exceed_review:")
            and f'"id":"{exceed_id}"' in item
            and '"result":"exceeded"' in item
            for item in gate["evidence"]
        )
        assert "reference_pack:none_supplied" in gate["evidence"]

        incomplete = deepcopy(review)
        incomplete["planned_exceed_results"].pop(exceed_id)
        with pytest.raises(QualityContractError, match="planned exceed.*every"):
            evaluate_quality_contract_review(
                session,
                pipeline_id,
                incomplete,
                project_root=project_root,
            )

        not_met = deepcopy(review)
        not_met["planned_exceed_results"][exceed_id] = {
            "result": "not_met",
            "difference": "信息层级仍与基线持平",
            "evidence": "Ian 逐项比较后记录",
        }
        assert evaluate_quality_contract_review(
            session,
            pipeline_id,
            not_met,
            project_root=project_root,
        )["status"] == "failed"


def test_frozen_reference_pack_requires_real_project_file_and_matching_hash(
    tmp_path: Path,
):
    root = tmp_path / "D" / "factory"
    with _session() as session:
        pipeline_id, project_root = _build_pipeline(session, root)
        snapshot = get_quality_contract(
            session,
            pipeline_id,
            project_root=project_root,
        )
        pipeline = session.get(ProductionPipeline, pipeline_id)
        assert pipeline is not None
        reference = Path(pipeline.project_dir) / "质量合同" / "references" / "ref.mp4"
        reference.parent.mkdir(parents=True)
        reference.write_bytes(b"frozen-reference")
        contract = deepcopy(snapshot.contract)
        contract["reference_pack"] = {
            "status": "frozen",
            "items": [
                {
                    "id": "ref_1",
                    "project_relative_path": "质量合同/references/ref.mp4",
                    "sha256": hashlib.sha256(reference.read_bytes()).hexdigest(),
                }
            ],
        }
        contract_path = Path(pipeline.project_dir) / snapshot.relative_path
        contract_path.write_text(
            json.dumps(contract, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        assert get_quality_contract(
            session,
            pipeline_id,
            project_root=project_root,
        ).contract["reference_pack"]["status"] == "frozen"

        reference.write_bytes(b"replaced-reference")
        with pytest.raises(QualityContractError, match="reference.*hash"):
            get_quality_contract(
                session,
                pipeline_id,
                project_root=project_root,
            )


def test_quality_contract_tampering_is_detected_instead_of_overwritten(tmp_path: Path):
    root = tmp_path / "D" / "factory"
    with _session() as session:
        pipeline_id, project_root = _build_pipeline(session, root)
        snapshot = ensure_default_quality_contract(
            session,
            pipeline_id,
            project_root=project_root,
        )
        pipeline = session.get(ProductionPipeline, pipeline_id)
        assert pipeline is not None
        contract_path = Path(pipeline.project_dir) / Path(snapshot.relative_path)
        contract_path.write_text('{"tampered":true}\n', encoding="utf-8")

        with pytest.raises(QualityContractError, match="invalid|changed"):
            ensure_default_quality_contract(
                session,
                pipeline_id,
                project_root=project_root,
            )
