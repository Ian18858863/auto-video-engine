"""Versioned, project-owned timeline truth for the editing module.

The legacy editing exporter still emits its compatible files, but this module
gives the project a stable internal representation that can be reopened after
restart.  It deliberately has no renderer dependency: renderers consume a
validated snapshot later in the pipeline.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any

from app.pipeline.project_identity import (
    PreparedProjectChildDirectoryUse,
    ProjectIdentityError,
    begin_prepared_project_child_directory_use,
    begin_prepared_project_directory_use,
    prepared_project_output_path,
)


TIMELINE_SPEC_ROOT = "时间线"
TIMELINE_SPEC_SCHEMA = 1
_VERSION_FILENAME_PATTERN = "ts-*.json"


class TimelineSpecError(ValueError):
    """Raised when a timeline cannot be validated or safely persisted."""


@dataclass(frozen=True, slots=True)
class TimelineSpecSnapshot:
    pipeline_id: int
    project_key: str
    version_id: str
    version_number: int
    input_hash: str
    tracks: tuple[dict[str, Any], ...]
    segments: tuple[dict[str, Any], ...]
    settings: dict[str, Any]
    source: str
    created_at: str
    artifacts: dict[str, Any] = field(default_factory=dict)
    parent_version_id: str = ""
    change_set: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": TIMELINE_SPEC_SCHEMA,
            "pipeline_id": self.pipeline_id,
            "project_key": self.project_key,
            "version_id": self.version_id,
            "version_number": self.version_number,
            "input_hash": self.input_hash,
            "tracks": [dict(item) for item in self.tracks],
            "segments": [dict(item) for item in self.segments],
            "settings": dict(self.settings),
            "artifacts": _json_value(self.artifacts),
            "parent_version_id": self.parent_version_id,
            "change_set": _json_value(self.change_set),
            "source": self.source,
            "created_at": self.created_at,
        }

    def public_dict(self) -> dict[str, Any]:
        return self.to_dict()


class TimelineSpecLedger:
    """Append-only version storage under one prepared D: project directory."""

    def __init__(self, pipeline: Any, *, project_root: Path):
        try:
            pipeline_id = int(getattr(pipeline, "id"))
        except (TypeError, ValueError) as exc:
            raise TimelineSpecError("TimelineSpec 缺少有效 pipeline_id") from exc
        if pipeline_id < 1:
            raise TimelineSpecError("TimelineSpec 缺少有效 pipeline_id")
        self.pipeline = pipeline
        self.pipeline_id = pipeline_id
        self.project_root = Path(project_root)
        if not self.project_root.is_absolute():
            raise TimelineSpecError("TimelineSpec project_root 必须是绝对路径")
        self.project_key = str(getattr(pipeline, "project_key", "") or "").strip()
        if not self.project_key:
            raise TimelineSpecError("TimelineSpec 缺少 project_key")

    def create(
        self,
        payload: Mapping[str, Any],
        *,
        source: str = "editing_export",
        change_base_version_id: str | None = None,
    ) -> TimelineSpecSnapshot:
        normalized = _normalize_payload(payload)
        input_hash = _sha256(normalized)
        versions = self.list()
        existing = next(
            (item for item in versions if item.input_hash == input_hash),
            None,
        )
        if existing is not None:
            return existing

        versions_use = self._directory_use("versions", create=True)
        version_number = max((item.version_number for item in versions), default=0) + 1
        version_id = f"ts-{self.pipeline_id}-{version_number:04d}-{input_hash[:12]}"
        parent = versions[-1] if versions else None
        change_base = parent
        if change_base_version_id is not None:
            requested_base = str(change_base_version_id).strip()
            change_base = next(
                (item for item in versions if item.version_id == requested_base),
                None,
            )
            if change_base is None:
                raise TimelineSpecError("TimelineSpec 变更比较基线不存在")
        snapshot = TimelineSpecSnapshot(
            pipeline_id=self.pipeline_id,
            project_key=self.project_key,
            version_id=version_id,
            version_number=version_number,
            input_hash=input_hash,
            tracks=tuple(normalized["tracks"]),
            segments=tuple(normalized["segments"]),
            settings=dict(normalized["settings"]),
            source=_safe_label(source, "editing_export"),
            created_at=_now_iso(),
            artifacts=dict(normalized.get("artifacts") or {}),
            parent_version_id=parent.version_id if parent is not None else "",
            change_set=_timeline_change_set(change_base, normalized),
        )
        _write_exclusive_snapshot(versions_use, snapshot)
        return snapshot

    def list(self) -> list[TimelineSpecSnapshot]:
        if not self._child_exists("versions"):
            return []
        versions_use = self._directory_use("versions", create=False)
        snapshots: list[TimelineSpecSnapshot] = []
        for path in sorted(versions_use.path.glob(_VERSION_FILENAME_PATTERN)):
            if not path.is_file() or path.is_symlink():
                raise TimelineSpecError("TimelineSpec 版本文件不是普通文件")
            versions_use.verify()
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise TimelineSpecError("TimelineSpec 版本文件无法读取") from exc
            snapshot = _snapshot_from_dict(raw)
            if (
                snapshot.pipeline_id != self.pipeline_id
                or snapshot.project_key != self.project_key
            ):
                raise TimelineSpecError("TimelineSpec 项目身份与当前项目不一致")
            snapshots.append(snapshot)
        numbers = [item.version_number for item in snapshots]
        if len(numbers) != len(set(numbers)):
            raise TimelineSpecError("TimelineSpec 版本号重复")
        return sorted(snapshots, key=lambda item: item.version_number)

    def get(self, version_id: str) -> TimelineSpecSnapshot:
        for item in self.list():
            if item.version_id == version_id:
                return item
        raise TimelineSpecError("TimelineSpec 版本不存在")

    def latest(self) -> TimelineSpecSnapshot | None:
        versions = self.list()
        return versions[-1] if versions else None

    def _directory_use(
        self,
        child: str,
        *,
        create: bool,
    ) -> PreparedProjectChildDirectoryUse:
        try:
            return begin_prepared_project_child_directory_use(
                self.pipeline,
                project_root=self.project_root,
                relative_path=Path(TIMELINE_SPEC_ROOT) / child,
                create=create,
            )
        except (ProjectIdentityError, OSError, ValueError) as exc:
            raise TimelineSpecError("TimelineSpec 目录未通过项目身份校验") from exc

    def _child_exists(self, child: str) -> bool:
        try:
            project_use = begin_prepared_project_directory_use(
                self.pipeline,
                project_root=self.project_root,
            )
            target = project_use.path / TIMELINE_SPEC_ROOT / child
            if not target.exists():
                return False
            if target.is_symlink() or not target.is_dir():
                raise TimelineSpecError("TimelineSpec 目录不是普通目录")
            project_use.verify()
            return True
        except (ProjectIdentityError, OSError, ValueError) as exc:
            raise TimelineSpecError("TimelineSpec 目录未通过项目身份校验") from exc


def build_timeline_spec_payload(
    timeline: Sequence[Mapping[str, Any]],
    *,
    selected_candidate_ids: Mapping[str, str] | None = None,
    settings: Mapping[str, Any] | None = None,
    artifacts: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Convert the compatible editing timeline into a deterministic spec."""

    if not isinstance(timeline, Sequence) or isinstance(
        timeline, (str, bytes, bytearray)
    ):
        raise TimelineSpecError("TimelineSpec segments 必须是列表")
    selected = {
        str(key): str(value)
        for key, value in (selected_candidate_ids or {}).items()
        if str(key).strip() and str(value).strip()
    }
    segments: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(timeline, start=1):
        if not isinstance(raw, Mapping):
            raise TimelineSpecError("TimelineSpec segment 必须是对象")
        segment_id = str(raw.get("segment_id") or raw.get("shot_id") or "").strip()
        if not segment_id:
            raise TimelineSpecError("TimelineSpec segment_id 是必需的")
        clip_id = f"{segment_id}:visual"
        if segment_id in seen_ids or clip_id in {item["clip_id"] for item in segments}:
            raise TimelineSpecError("TimelineSpec segment_id 必须唯一")
        start_seconds = _time_seconds(raw.get("start"), "start")
        end_seconds = _time_seconds(raw.get("end"), "end")
        if end_seconds <= start_seconds:
            raise TimelineSpecError("TimelineSpec end 必须晚于 start")
        item = {
            "clip_id": clip_id,
            "segment_id": segment_id,
            "track_id": "video-main",
            # Preserve a numeric zero instead of turning it into an empty
            # timestamp through ``value or \"\"``.  Zero is the normal start
            # time of the first shot and must remain part of the stable hash.
            "start": _text_value(raw.get("start")),
            "end": _text_value(raw.get("end")),
            "start_seconds": start_seconds,
            "end_seconds": end_seconds,
            "z_index": 0,
            "visual_type": str(raw.get("visual_type") or ""),
            "visual_direction": str(raw.get("visual_direction") or ""),
            "voiceover": str(raw.get("voiceover") or ""),
            "material_path": str(raw.get("material_path") or ""),
            "source_url": str(raw.get("source_url") or ""),
            "evidence_kind": str(raw.get("evidence_kind") or ""),
            "material_status": str(raw.get("material_status") or ""),
            "selected_candidate_id": selected.get(segment_id, ""),
            "transform": {"fit": "cover", "position": "center"},
            "subtitle_strategy": str(raw.get("subtitle_strategy") or "跟随口播"),
            "edit_instruction": str(raw.get("edit_instruction") or ""),
        }
        segments.append(item)
        seen_ids.add(segment_id)

    normalized_settings = {
        "width": 1080,
        "height": 1920,
        "fps": 30,
        "sample_rate": 48000,
        "aspect_ratio": "9:16",
    }
    if settings:
        for key, value in settings.items():
            normalized_settings[str(key)] = _json_value(value)
    tracks = [
        {
            "track_id": "video-main",
            "kind": "video",
            "order": 0,
            "clip_ids": [item["clip_id"] for item in segments],
        },
        {
            "track_id": "voiceover-main",
            "kind": "voiceover",
            "order": 1,
            "clip_ids": [],
        },
        {
            "track_id": "subtitle-main",
            "kind": "subtitle",
            "order": 2,
            "clip_ids": [],
        },
    ]
    return {
        "tracks": tracks,
        "segments": segments,
        "settings": normalized_settings,
        "artifacts": _normalize_artifacts(artifacts),
    }


def _normalize_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise TimelineSpecError("TimelineSpec 内容必须是对象")
    tracks = payload.get("tracks")
    segments = payload.get("segments")
    settings = payload.get("settings")
    artifacts = payload.get("artifacts")
    # The public create seam accepts a concise ``{"segments": [...]}``
    # payload and derives the canonical tracks/settings.  Persisted snapshots
    # always carry all three fields and take the strict path below.
    if tracks is None and isinstance(segments, Sequence) and not isinstance(
        segments, (str, bytes, bytearray)
    ):
        selected = {
            str(item.get("segment_id") or item.get("shot_id")): str(
                item.get("selected_candidate_id") or ""
            )
            for item in segments
            if isinstance(item, Mapping) and item.get("selected_candidate_id")
        }
        return _normalize_payload(
            build_timeline_spec_payload(
                [item for item in segments if isinstance(item, Mapping)],
                selected_candidate_ids=selected,
                settings=settings if isinstance(settings, Mapping) else None,
                artifacts=artifacts if isinstance(artifacts, Mapping) else None,
            )
        )
    if not isinstance(tracks, Sequence) or isinstance(tracks, (str, bytes, bytearray)):
        raise TimelineSpecError("TimelineSpec tracks 必须是列表")
    if not isinstance(segments, Sequence) or isinstance(segments, (str, bytes, bytearray)):
        raise TimelineSpecError("TimelineSpec segments 必须是列表")
    if not isinstance(settings, Mapping):
        raise TimelineSpecError("TimelineSpec settings 必须是对象")
    selected = {
        str(item.get("segment_id") or item.get("shot_id")): str(
            item.get("selected_candidate_id") or ""
        )
        for item in segments
        if isinstance(item, Mapping) and item.get("selected_candidate_id")
    }
    normalized_segments = build_timeline_spec_payload(
        [item for item in segments if isinstance(item, Mapping)],
        selected_candidate_ids=selected,
        settings=settings,
        artifacts=artifacts if isinstance(artifacts, Mapping) else None,
    )["segments"]
    if len(normalized_segments) != len(segments):
        raise TimelineSpecError("TimelineSpec segment 必须是对象")
    normalized_tracks = []
    track_ids: set[str] = set()
    for raw in tracks:
        if not isinstance(raw, Mapping):
            raise TimelineSpecError("TimelineSpec track 必须是对象")
        track_id = str(raw.get("track_id") or "").strip()
        if not track_id or track_id in track_ids:
            raise TimelineSpecError("TimelineSpec track_id 必须唯一")
        clip_ids = raw.get("clip_ids", [])
        if not isinstance(clip_ids, Sequence) or isinstance(clip_ids, (str, bytes, bytearray)):
            raise TimelineSpecError("TimelineSpec clip_ids 必须是列表")
        normalized_tracks.append(
            {
                "track_id": track_id,
                "kind": str(raw.get("kind") or "").strip(),
                "order": int(raw.get("order", len(normalized_tracks))),
                "clip_ids": [str(item) for item in clip_ids],
            }
        )
        track_ids.add(track_id)
    if not normalized_segments:
        raise TimelineSpecError("TimelineSpec 至少需要一个 segment")
    known_clips = {item["clip_id"] for item in normalized_segments}
    if any(
        clip_id not in known_clips
        for track in normalized_tracks
        for clip_id in track["clip_ids"]
    ):
        raise TimelineSpecError("TimelineSpec track 引用了不存在的 clip")
    return {
        "tracks": normalized_tracks,
        "segments": normalized_segments,
        "settings": {str(key): _json_value(value) for key, value in settings.items()},
        "artifacts": _normalize_artifacts(artifacts),
    }


def _snapshot_from_dict(raw: Any) -> TimelineSpecSnapshot:
    if not isinstance(raw, Mapping) or raw.get("schema_version") != TIMELINE_SPEC_SCHEMA:
        raise TimelineSpecError("TimelineSpec schema 无效")
    payload = _normalize_payload(raw)
    expected_hash = _sha256(payload)
    if raw.get("input_hash") != expected_hash:
        raise TimelineSpecError("TimelineSpec 哈希不匹配，疑似篡改")
    pipeline_id = _positive_int(raw.get("pipeline_id"), "pipeline_id")
    version_number = _positive_int(raw.get("version_number"), "version_number")
    version_id = str(raw.get("version_id") or "")
    if not version_id.startswith(f"ts-{pipeline_id}-{version_number:04d}-"):
        raise TimelineSpecError("TimelineSpec ID 不匹配")
    project_key = str(raw.get("project_key") or "").strip()
    if not project_key:
        raise TimelineSpecError("TimelineSpec 缺少 project_key")
    input_hash = raw.get("input_hash")
    if not isinstance(input_hash, str) or len(input_hash) != 64:
        raise TimelineSpecError("TimelineSpec input_hash 无效")
    return TimelineSpecSnapshot(
        pipeline_id=pipeline_id,
        project_key=project_key,
        version_id=version_id,
        version_number=version_number,
        input_hash=input_hash,
        tracks=tuple(dict(item) for item in payload["tracks"]),
        segments=tuple(dict(item) for item in payload["segments"]),
        settings=dict(payload["settings"]),
        source=_safe_label(raw.get("source"), "unknown"),
        created_at=str(raw.get("created_at") or ""),
        artifacts=dict(payload.get("artifacts") or {}),
        parent_version_id=str(raw.get("parent_version_id") or "").strip(),
        change_set=_normalize_change_set(raw.get("change_set")),
    )


def load_timeline_spec_snapshot(raw: Mapping[str, Any]) -> TimelineSpecSnapshot:
    """Public, fail-closed parser for adapters that consume a snapshot.

    Keeping this seam beside the ledger makes the hash/schema contract explicit
    without requiring downstream renderers to import a private helper.
    """

    return _snapshot_from_dict(raw)


def timeline_spec_input_hash(payload: Mapping[str, Any]) -> str:
    """Return the exact hash used by the append-only ledger."""

    return _sha256(_normalize_payload(payload))


def timeline_spec_edit_graph_hash(payload: Mapping[str, Any]) -> str:
    """Hash edit/render inputs while deliberately ignoring bound media artifacts."""

    normalized = _normalize_payload(payload)
    return _sha256(
        {
            "tracks": normalized["tracks"],
            "segments": normalized["segments"],
            "settings": normalized["settings"],
        }
    )


def _timeline_change_set(
    parent: TimelineSpecSnapshot | None,
    normalized: Mapping[str, Any],
) -> dict[str, Any]:
    desired_segments = [dict(item) for item in normalized.get("segments", [])]
    desired_ids = [str(item.get("segment_id") or "") for item in desired_segments]
    if parent is None:
        return {
            "affected_segment_ids": [item for item in desired_ids if item],
            "change_kinds": ["initial"],
            "change_base_version_id": "",
        }
    previous_payload = {
        "tracks": [dict(item) for item in parent.tracks],
        "segments": [dict(item) for item in parent.segments],
        "settings": dict(parent.settings),
        "artifacts": dict(parent.artifacts),
    }
    change_kinds: list[str] = []
    if previous_payload["tracks"] != normalized.get("tracks"):
        change_kinds.append("tracks")
    if previous_payload["settings"] != normalized.get("settings"):
        change_kinds.append("settings")
    if previous_payload["artifacts"] != normalized.get("artifacts"):
        change_kinds.append("artifacts")
    previous_segments = previous_payload["segments"]
    previous_by_id = {
        str(item.get("segment_id") or ""): item for item in previous_segments
    }
    desired_by_id = {
        str(item.get("segment_id") or ""): item for item in desired_segments
    }
    previous_ids = [str(item.get("segment_id") or "") for item in previous_segments]
    ordered_ids = list(dict.fromkeys([*desired_ids, *previous_ids]))
    if "tracks" in change_kinds or "settings" in change_kinds:
        affected = [item for item in ordered_ids if item]
    else:
        affected = [
            segment_id
            for segment_id in ordered_ids
            if segment_id
            and previous_by_id.get(segment_id) != desired_by_id.get(segment_id)
        ]
    if affected:
        change_kinds.insert(0, "segments")
    return {
        "affected_segment_ids": affected,
        "change_kinds": change_kinds,
        "change_base_version_id": parent.version_id,
    }


def _normalize_change_set(value: Any) -> dict[str, Any]:
    if value is None:
        return {
            "affected_segment_ids": [],
            "change_kinds": [],
            "change_base_version_id": "",
        }
    if not isinstance(value, Mapping):
        raise TimelineSpecError("TimelineSpec change_set 必须是对象")
    affected = value.get("affected_segment_ids", [])
    kinds = value.get("change_kinds", [])
    if (
        not isinstance(affected, Sequence)
        or isinstance(affected, (str, bytes, bytearray))
        or not isinstance(kinds, Sequence)
        or isinstance(kinds, (str, bytes, bytearray))
    ):
        raise TimelineSpecError("TimelineSpec change_set 列表无效")
    affected_ids = [str(item).strip() for item in affected]
    change_kinds = [str(item).strip() for item in kinds]
    change_base_version_id = str(value.get("change_base_version_id") or "").strip()
    if (
        any(not item for item in affected_ids)
        or any(not item for item in change_kinds)
        or len(set(affected_ids)) != len(affected_ids)
        or len(set(change_kinds)) != len(change_kinds)
    ):
        raise TimelineSpecError("TimelineSpec change_set 内容无效")
    return {
        "affected_segment_ids": affected_ids,
        "change_kinds": change_kinds,
        "change_base_version_id": change_base_version_id,
    }


def _write_exclusive_snapshot(
    directory_use: PreparedProjectChildDirectoryUse,
    snapshot: TimelineSpecSnapshot,
) -> None:
    path = prepared_project_output_path(directory_use, f"{snapshot.version_id}.json")
    encoded = _canonical_bytes(snapshot.to_dict())
    try:
        with path.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        try:
            if path.read_bytes() != encoded:
                raise TimelineSpecError("TimelineSpec 文件碰撞或被篡改")
        except OSError as exc:
            raise TimelineSpecError("TimelineSpec 文件碰撞且无法验证") from exc
    except OSError as exc:
        raise TimelineSpecError("TimelineSpec 写入失败") from exc
    directory_use.verify()


def _time_seconds(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise TimelineSpecError(f"TimelineSpec {label} 无效")
    if isinstance(value, (int, float)):
        result = float(value)
    else:
        text = str(value or "").strip()
        parts = text.split(":")
        try:
            if len(parts) == 2:
                result = float(parts[0]) * 60 + float(parts[1])
            elif len(parts) == 3:
                result = float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
            else:
                result = float(text)
        except (TypeError, ValueError) as exc:
            raise TimelineSpecError(f"TimelineSpec {label} 无效") from exc
    if result < 0:
        raise TimelineSpecError(f"TimelineSpec {label} 无效")
    return round(result, 3)


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_value(val) for key, val in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TimelineSpecError("TimelineSpec 包含不可序列化字段")


def _normalize_artifacts(value: Any) -> dict[str, Any]:
    """Validate optional real-media references without storing absolute paths."""

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TimelineSpecError("TimelineSpec artifacts 必须是对象")
    normalized = _json_value(value)
    if not isinstance(normalized, dict):
        raise TimelineSpecError("TimelineSpec artifacts 必须是对象")
    # ``audio`` is accepted as an input alias; the persisted contract calls it
    # ``voiceover`` so the track name is unambiguous.
    if "audio" in normalized and "voiceover" not in normalized:
        normalized["voiceover"] = normalized.pop("audio")
    for key in ("voiceover", "subtitles"):
        entry = normalized.get(key)
        if entry is None:
            continue
        if not isinstance(entry, dict):
            raise TimelineSpecError(f"TimelineSpec {key} artifact 必须是对象")
        path = str(entry.get("project_relative_path") or "").replace("\\", "/")
        if not path or path.startswith("/") or ":" in path:
            raise TimelineSpecError(f"TimelineSpec {key} artifact 必须使用项目相对路径")
        parts = [part for part in path.split("/") if part]
        if not parts or any(part in {".", ".."} for part in parts):
            raise TimelineSpecError(f"TimelineSpec {key} artifact 路径无效")
        digest = str(entry.get("sha256") or "")
        if len(digest) != 64 or any(char not in "0123456789abcdefABCDEF" for char in digest):
            raise TimelineSpecError(f"TimelineSpec {key} artifact sha256 无效")
        mime = str(entry.get("mime") or "").strip()
        if not mime:
            raise TimelineSpecError(f"TimelineSpec {key} artifact mime 必填")
        if key == "voiceover":
            try:
                duration = float(entry.get("duration_seconds"))
            except (TypeError, ValueError):
                duration = -1.0
            if not math.isfinite(duration) or duration <= 0:
                raise TimelineSpecError("TimelineSpec voiceover duration 无效")
        entry["project_relative_path"] = "/".join(parts)
        entry["sha256"] = digest.lower()
        entry["mime"] = mime
    return normalized


def _canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
        "utf-8"
    )


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _positive_int(value: Any, label: str) -> int:
    if type(value) is not int or value < 1:
        raise TimelineSpecError(f"TimelineSpec {label} 无效")
    return value


def _safe_label(value: Any, default: str) -> str:
    text = str(value or "").strip()
    return text[:120] or default


def _text_value(value: Any) -> str:
    return "" if value is None else str(value)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
