"""Read-only quality gate between candidate review and material exports."""

from __future__ import annotations

import copy
from collections.abc import Sequence
from typing import Any

from app.models import ProductionPipeline
from app.pipeline.material_candidate_contract import (
    CandidateContractError,
    CandidateSet,
)
from app.pipeline.material_candidate_media import revalidate_candidate_media
from app.pipeline.material_selection_invalidation import (
    MaterialSelectionDependencyError,
    assert_material_selection_dependency_current,
)


def assert_pipeline_candidate_review_ready(
    pipeline: ProductionPipeline,
    shot_ids: Sequence[str],
) -> dict[str, CandidateSet]:
    """Require a valid pool and one explicit lock for every current shot.

    Legacy pipelines without a ``candidate_pool`` key remain compatible. Once
    candidate review is enabled, rebuilding each immutable ``CandidateSet``
    also rechecks preview existence, rights, evidence and source diversity
    immediately before downstream files are created.
    """

    step = next(
        (item for item in pipeline.steps if item.name == "生成素材任务"),
        None,
    )
    payload = step.output_summary if step else {}
    try:
        assert_material_selection_dependency_current(payload, pipeline.script_asset)
    except MaterialSelectionDependencyError as exc:
        raise CandidateContractError(
            f"素材锁定已过期：{exc} 请重新生成并确认素材后再导出。"
        ) from exc
    if not isinstance(payload, dict) or "candidate_pool" not in payload:
        return {}

    pools = _candidate_pool_map(payload.get("candidate_pool"))
    normalized_shot_ids = _normalize_shot_ids(shot_ids)
    if not normalized_shot_ids:
        raise CandidateContractError("素材候选池存在，但素材任务为空，不能导出。")

    current_ids = set(normalized_shot_ids)
    stale_ids = set(pools) - current_ids
    if stale_ids:
        stale = "、".join(sorted(stale_ids))
        raise CandidateContractError(
            f"素材候选池包含已失效镜头（{stale}），请重新生成后再导出。"
        )

    states: dict[str, CandidateSet] = {}
    for shot_id in normalized_shot_ids:
        value = pools.get(shot_id)
        if value is None:
            raise CandidateContractError(
                f"镜头 {shot_id} 尚未建立素材候选池，不能导出。"
            )
        state = CandidateSet.from_dict(value)
        if state.shot_id != shot_id:
            raise CandidateContractError("candidate pool shot_id does not match task")
        if state.locked_candidate is None:
            raise CandidateContractError(
                f"镜头 {shot_id} 的素材候选尚未锁定，不能导出。"
            )
        locked = state.locked_candidate
        revalidate_candidate_media(locked)
        if locked.rights_evidence is not None:
            review_status = locked.rights_evidence["rights_review"]["status"]
            if review_status != "cleared_for_intended_use":
                raise CandidateContractError(
                    f"镜头 {shot_id} 的锁定素材尚未完成本次用途清权，不能导出。"
                )
        states[shot_id] = state
    return states


def _normalize_shot_ids(shot_ids: Sequence[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in shot_ids:
        shot_id = str(value or "").strip()
        if not shot_id:
            raise CandidateContractError("material task shot_id is required")
        if shot_id in seen:
            raise CandidateContractError(f"duplicate material task shot_id: {shot_id}")
        seen.add(shot_id)
        result.append(shot_id)
    return result


def _candidate_pool_map(raw: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, dict):
        raise CandidateContractError("candidate_pool must be an object")
    if "shot_id" in raw and "candidates" in raw:
        shot_id = raw.get("shot_id")
        if not isinstance(shot_id, str) or not shot_id.strip():
            raise CandidateContractError("candidate_pool shot_id is required")
        return {shot_id.strip(): copy.deepcopy(raw)}

    result: dict[str, dict[str, Any]] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key.strip():
            raise CandidateContractError("candidate_pool keys must be shot IDs")
        if not isinstance(value, dict):
            raise CandidateContractError("candidate_pool values must be objects")
        result[key.strip()] = copy.deepcopy(value)
    return result
