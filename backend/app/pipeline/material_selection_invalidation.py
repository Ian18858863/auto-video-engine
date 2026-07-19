"""Material-selection dependency fingerprints and append-only stale history.

This module is deliberately independent from review/rework.  It protects a
material choice when the script contract that justified it changes: the old
lock remains inspectable as stale history, but it cannot be used as if it were
current.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence


STALE_REASON_CODE = "upstream_script_changed"
STALE_REASON = "上游脚本已经变化；旧锁定仅保留为历史，需重新生成并确认素材。"


class MaterialSelectionDependencyError(ValueError):
    pass


def material_input_sha256(
    *,
    voiceover: Any,
    storyboard: Any,
    animation_prompts: Any,
    use_avatar: Any,
) -> str:
    """Hash only the script inputs that can change material selection.

    Titles, cover copy, publish copy, subtitles and the separate avatar overlay
    choice are intentionally excluded. Editing those fields must not silently
    invalidate a reviewed visual choice.
    """

    payload = {
        "voiceover": str(voiceover or ""),
        "storyboard": _dict_rows(storyboard),
        "animation_prompts": _dict_rows(animation_prompts),
    }
    _ = use_avatar
    return _json_sha256(payload)


def material_input_sha256_for_script(script: Any) -> str:
    return material_input_sha256(
        voiceover=getattr(script, "voiceover", ""),
        storyboard=getattr(script, "storyboard", []),
        animation_prompts=getattr(script, "animation_prompts", []),
        use_avatar=_script_use_avatar_value(script),
    )


def invalidate_material_selection_payload(
    payload: Mapping[str, Any] | None,
    *,
    target_script_version_id: str,
    target_script_version_hash: str,
    target_material_input_sha256: str,
    invalidated_at: str | None = None,
) -> dict[str, Any]:
    """Return a stale, audit-preserving copy of one material-step payload.

    Repeated calls while the same selection is already stale do not duplicate
    its immutable history entry.  They may update only the top-level target
    metadata so the UI can point at the newest draft.
    """

    current = copy.deepcopy(dict(payload or {}))
    if not _has_material_state(current):
        return {}
    stale_at = invalidated_at or datetime.now(timezone.utc).isoformat()
    already_stale = current.get("material_selection_state") == "stale"

    if not already_stale:
        snapshot = _selection_snapshot(current)
        snapshot_sha256 = _json_sha256(snapshot)
        entry: dict[str, Any] = {
            "event": "material_selection_stale",
            "reason_code": STALE_REASON_CODE,
            "reason": STALE_REASON,
            "stale_at": stale_at,
            "source_script_version_id": str(current.get("script_version_id") or ""),
            "source_script_version_hash": str(current.get("script_version_hash") or ""),
            "source_material_input_sha256": str(
                current.get("material_input_sha256") or snapshot_sha256
            ),
            "target_script_version_id": str(target_script_version_id or ""),
            "target_script_version_hash": str(target_script_version_hash or ""),
            "target_material_input_sha256": str(target_material_input_sha256 or ""),
            "locked_selections": _locked_selections(current),
            "candidate_pool_snapshot": snapshot["candidate_pool"],
            "candidate_selection_history": snapshot["candidate_selection_history"],
            "candidate_generation_history": snapshot["candidate_generation_history"],
            "snapshot_sha256": snapshot_sha256,
        }
        entry["entry_sha256"] = _json_sha256(entry)
        history = _history_rows(current.get("material_selection_history"))
        current["material_selection_history"] = [*history, entry]

    current["material_selection_state"] = "stale"
    current["material_selection_stale_reason_code"] = STALE_REASON_CODE
    current["material_selection_stale_reason"] = STALE_REASON
    current["material_selection_stale_at"] = str(
        current.get("material_selection_stale_at") or stale_at
    )
    current["target_script_version_id"] = str(target_script_version_id or "")
    current["target_script_version_hash"] = str(target_script_version_hash or "")
    current["target_material_input_sha256"] = str(target_material_input_sha256 or "")

    pools = current.get("candidate_pool")
    if isinstance(pools, dict):
        for pool in _pool_values(pools):
            pool["stale"] = True
            pool["stale_reason_code"] = STALE_REASON_CODE
            pool["stale_reason"] = STALE_REASON
            pool["stale_at"] = str(current["material_selection_stale_at"])
            pool["upstream_script_version_id"] = str(
                current.get("script_version_id") or ""
            )
            pool["upstream_script_version_hash"] = str(
                current.get("script_version_hash") or ""
            )
            pool["target_script_version_id"] = str(target_script_version_id or "")
            pool["target_script_version_hash"] = str(target_script_version_hash or "")
            pool["target_material_input_sha256"] = str(
                target_material_input_sha256 or ""
            )
    return current


def material_selection_history(payload: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(payload, Mapping):
        return []
    return _history_rows(payload.get("material_selection_history"))


def is_material_selection_stale(payload: Mapping[str, Any] | None) -> bool:
    return bool(
        isinstance(payload, Mapping)
        and payload.get("material_selection_state") == "stale"
    )


def assert_material_selection_dependency_current(
    payload: Mapping[str, Any] | None,
    script: Any,
) -> None:
    """Fail closed when a stale marker was missed by an upstream writer."""

    if not isinstance(payload, Mapping) or not _has_material_state(payload):
        return
    if is_material_selection_stale(payload):
        raise MaterialSelectionDependencyError(STALE_REASON)
    persisted = str(payload.get("material_input_sha256") or "").strip().lower()
    if len(persisted) != 64 or any(char not in "0123456789abcdef" for char in persisted):
        raise MaterialSelectionDependencyError(
            "素材任务缺少可验证的上游输入哈希，不能继续使用现有锁定。"
        )
    if script is None:
        raise MaterialSelectionDependencyError(
            "素材任务找不到当前脚本依赖，不能继续使用现有锁定。"
        )
    current = material_input_sha256_for_script(script)
    if persisted != current:
        raise MaterialSelectionDependencyError(
            "素材任务绑定的脚本输入已经变化，旧锁定不能继续使用。"
        )


def _selection_snapshot(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "script_version_id": str(payload.get("script_version_id") or ""),
        "script_version_hash": str(payload.get("script_version_hash") or ""),
        "material_input_sha256": str(payload.get("material_input_sha256") or ""),
        "material_tasks": _audit_safe_rows(payload.get("material_tasks")),
        "candidate_pool": _sanitized_candidate_pools(payload.get("candidate_pool")),
        "candidate_selection_history": _history_map(
            payload.get("candidate_selection_history")
        ),
        "candidate_generation_history": _history_rows(
            payload.get("candidate_generation_history")
        ),
    }


def _locked_selections(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    locked: list[dict[str, Any]] = []
    pools = _sanitized_candidate_pools(payload.get("candidate_pool"))
    for shot_id, pool in pools.items():
        candidates = pool.get("candidates")
        if not isinstance(candidates, list):
            continue
        for candidate in candidates:
            if not isinstance(candidate, dict) or candidate.get("status") != "locked":
                continue
            locked.append(
                {
                    "shot_id": shot_id,
                    "candidate_id": str(candidate.get("candidate_id") or ""),
                    "revision": pool.get("revision", 0),
                    "reason": str(candidate.get("reason") or ""),
                    "source": str(candidate.get("source") or ""),
                    "url": str(candidate.get("url") or ""),
                    "rights": str(candidate.get("rights") or ""),
                    "evidence_kind": str(candidate.get("evidence_kind") or ""),
                    "media_sha256": str(
                        (candidate.get("media_validation") or {}).get("sha256")
                        if isinstance(candidate.get("media_validation"), dict)
                        else ""
                    ),
                }
            )
    return locked


def _sanitized_candidate_pools(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        return {}
    if "shot_id" in value and "candidates" in value:
        shot_id = str(value.get("shot_id") or "")
        source = {shot_id: value} if shot_id else {}
    else:
        source = value
    result: dict[str, dict[str, Any]] = {}
    for raw_shot_id, raw_pool in source.items():
        shot_id = str(raw_shot_id or "").strip()
        if not shot_id or not isinstance(raw_pool, dict):
            continue
        candidates: list[dict[str, Any]] = []
        for raw_candidate in raw_pool.get("candidates", []):
            if not isinstance(raw_candidate, dict):
                continue
            candidate = {
                key: copy.deepcopy(value)
                for key, value in raw_candidate.items()
                if key != "preview_path"
            }
            candidates.append(candidate)
        result[shot_id] = {
            "shot_id": str(raw_pool.get("shot_id") or shot_id),
            "revision": raw_pool.get("revision", 0),
            "candidates": candidates,
        }
    return result


def _pool_values(pools: dict[str, Any]) -> list[dict[str, Any]]:
    if "shot_id" in pools and "candidates" in pools:
        return [pools]
    return [value for value in pools.values() if isinstance(value, dict)]


def _history_rows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [copy.deepcopy(item) for item in value if isinstance(item, dict)]


def _history_map(value: Any) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key): _history_rows(rows)
        for key, rows in value.items()
        if isinstance(key, str)
    }


def _dict_rows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [copy.deepcopy(dict(item)) for item in value if isinstance(item, Mapping)]


def _audit_safe_rows(value: Any) -> list[dict[str, Any]]:
    """Keep useful history while refusing physical filesystem locations."""

    return [
        cleaned
        for item in _dict_rows(value)
        if isinstance((cleaned := _audit_safe_value(item)), dict)
    ]


def _audit_safe_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): cleaned
            for key, item in value.items()
            if (cleaned := _audit_safe_value(item)) is not None
        }
    if isinstance(value, list):
        return [
            cleaned
            for item in value
            if (cleaned := _audit_safe_value(item)) is not None
        ]
    if isinstance(value, str) and _ABSOLUTE_PATH_PATTERN.search(value):
        return None
    return copy.deepcopy(value)


def _has_material_state(payload: Mapping[str, Any]) -> bool:
    return any(
        bool(payload.get(key))
        for key in (
            "material_tasks",
            "candidate_pool",
            "candidate_selection_history",
            "candidate_generation_history",
            "material_selection_history",
        )
    )


def _script_use_avatar_value(script: Any) -> bool:
    for item in getattr(script, "edit_checklist", None) or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "presentation_options":
            return bool(item.get("use_avatar", True))
    return True


def _json_sha256(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


_ABSOLUTE_PATH_PATTERN = re.compile(
    r"(?i)(?:^|[\s\"'(])(?:[a-z]:[\\/]|\\\\[^\\/]+[\\/])"
)
