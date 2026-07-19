"""Quality-report writing and explicit Ian approval.

This module is the release-boundary adapter for the refactor.  It writes an
immutable, schema-compatible quality report under the prepared D: project and
keeps the approval mutation separate.  A report can be generated with failed
manual gates; only a fresh report with every gate passed and an explicit
``approved_by == "Ian"`` request may create completion evidence.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
from typing import Any, Iterator, Mapping
from uuid import uuid4

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.core.config import get_settings, resolve_configured_path
from app.models import ProductionPipeline
from app.pipeline.completion_policy import (
    _GuardedFileMissing,
    _GuardedFileTooLarge,
    _GuardedFileUnsafe,
    _MAX_REPORT_BYTES,
    _is_strict_json,
    _read_artifact,
    _read_report,
    _read_with_guard,
    _cross_handle_snapshot,
    _open_guarded_read_handle,
    _parse_quality_contract_gate_evidence,
    _safe_regular_snapshot,
    _stat_snapshot,
    evaluate_pipeline_completion,
    pipeline_state_sha256,
)
from app.pipeline.project_identity import (
    ProjectIdentityError,
    begin_prepared_project_child_directory_use,
    begin_prepared_project_directory_use,
    prepared_project_output_path,
)
from app.pipeline.quality_contract_service import (
    QualityContractError,
    evaluate_quality_contract_review,
)
from app.pipeline.timeline_spec import TimelineSpecLedger, TimelineSpecError
from app.video.technical_probe import (
    VideoTechnicalExpectation,
    VideoTechnicalProbeError,
    VideoTechnicalReport,
    probe_video,
)


REPORT_FORMAT = "auto-video-engine-quality-report-v1"
_GATE_NAMES = (
    "structure",
    "technical",
    "visual_narration",
    "facts_rights",
    "quality_contract",
)
_REPORT_DIR = Path("\u6210\u7247") / "quality-reports"
_FINISHED_DIR = "\u6210\u7247"
_RELATIVE_MP4 = re.compile(rf"^{re.escape(_FINISHED_DIR)}(?:/[^/]+)+\.mp4$")
_RELATIVE_JSON = re.compile(rf"^{re.escape(_FINISHED_DIR)}(?:/[^/]+)+\.json$")
_MAX_MANUAL_EVIDENCE_ITEMS = 32
_MAX_MANUAL_EVIDENCE_LENGTH = 2_000


class QualityReportError(ValueError):
    """A quality report or approval request failed closed."""


class QualityApprovalConflictError(QualityReportError):
    """Another writer changed completion evidence before this approval committed."""


class QualityApprovalCommitUnknownError(RuntimeError):
    """The DB commit acknowledgement was lost; evidence must be reconciled."""


class QualityApprovalReconcileError(RuntimeError):
    """Approval committed or may have committed, but fresh verification failed."""


@dataclass(frozen=True, slots=True)
class QualityReportResult:
    pipeline_id: int
    artifact_relative_path: str
    artifact_sha256: str
    report_relative_path: str
    report_sha256: str
    report: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "pipeline_id": self.pipeline_id,
            "artifact_relative_path": self.artifact_relative_path,
            "artifact_sha256": self.artifact_sha256,
            "quality_report_relative_path": self.report_relative_path,
            "quality_report_sha256": self.report_sha256,
            "report": deepcopy(self.report),
            "approval_required": True,
            "publish_supported": False,
        }


@dataclass(frozen=True, slots=True)
class QualityApprovalResult:
    pipeline_id: int
    evidence: dict[str, Any]
    decision: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "pipeline_id": self.pipeline_id,
            "evidence": deepcopy(self.evidence),
            "decision": deepcopy(self.decision),
            "publish_supported": bool(self.decision.get("is_complete")),
        }


@dataclass(frozen=True, slots=True)
class QualityArtifactCandidate:
    artifact_relative_path: str
    artifact_sha256: str
    size_bytes: int
    modified_at: str
    timeline_version_id: str
    timeline_input_hash: str
    timeline_status: str
    can_quality_check: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_relative_path": self.artifact_relative_path,
            "artifact_sha256": self.artifact_sha256,
            "size_bytes": self.size_bytes,
            "modified_at": self.modified_at,
            "timeline_version_id": self.timeline_version_id,
            "timeline_input_hash": self.timeline_input_hash,
            "timeline_status": self.timeline_status,
            "can_quality_check": self.can_quality_check,
        }


def list_quality_artifact_candidates(
    session: Session,
    pipeline_id: int,
    *,
    project_root: Path | None = None,
    limit: int = 20,
) -> list[QualityArtifactCandidate]:
    """List recent modern renderer outputs without exposing physical paths."""

    if type(limit) is not int or not 1 <= limit <= 50:
        raise QualityReportError("quality artifact candidate limit is invalid")
    pipeline, _project_dir, approved_root = _pipeline_context(
        session,
        pipeline_id,
        project_root=project_root,
    )
    current_timeline = _latest_timeline(pipeline, approved_root)
    current_version_id = str(
        getattr(current_timeline, "version_id", "") or ""
    ).strip()
    current_input_hash = str(
        getattr(current_timeline, "input_hash", "") or ""
    ).casefold()
    versions_relative = Path("成片") / "versions"
    try:
        versions_use = begin_prepared_project_child_directory_use(
            pipeline,
            project_root=approved_root,
            relative_path=versions_relative,
            create=False,
        )
    except (ProjectIdentityError, OSError, ValueError):
        return []
    candidates: list[tuple[int, QualityArtifactCandidate]] = []
    try:
        entries = list(versions_use.path.iterdir())
        versions_use.verify()
    except (OSError, ProjectIdentityError):
        return []
    for entry in entries:
        if (
            not entry.is_dir()
            or entry.is_symlink()
            or entry.name in {".", ".."}
            or "/" in entry.name
            or "\\" in entry.name
        ):
            continue
        relative = PurePosixPath(
            versions_relative.as_posix(), entry.name, "rendered.mp4"
        ).as_posix()
        try:
            artifact_path, artifact_relative = _resolve_project_file(
                pipeline,
                approved_root,
                relative,
                suffix=".mp4",
            )
            artifact_sha, artifact_is_mp4 = _guarded_artifact_snapshot(
                pipeline, artifact_relative
            )
            if not artifact_is_mp4:
                continue
            timeline_version_id, timeline_input_hash = _verified_artifact_lineage(
                pipeline,
                approved_root,
                artifact_relative=artifact_relative,
                artifact_sha256=artifact_sha,
            )
            is_current = (
                timeline_version_id == current_version_id
                and timeline_input_hash == current_input_hash
            )
            metadata = artifact_path.stat()
            versions_use.verify()
        except (QualityReportError, OSError, ProjectIdentityError, ValueError):
            continue
        candidates.append(
            (
                int(metadata.st_mtime_ns),
                QualityArtifactCandidate(
                    artifact_relative_path=artifact_relative,
                    artifact_sha256=artifact_sha,
                    size_bytes=int(metadata.st_size),
                    modified_at=datetime.fromtimestamp(
                        metadata.st_mtime, tz=timezone.utc
                    ).isoformat(),
                    timeline_version_id=timeline_version_id,
                    timeline_input_hash=timeline_input_hash,
                    timeline_status="current" if is_current else "historical",
                    can_quality_check=is_current,
                ),
            )
        )
    candidates.sort(key=lambda item: item[0], reverse=True)
    return [item[1] for item in candidates[:limit]]


def resolve_quality_artifact_path(
    session: Session,
    pipeline_id: int,
    artifact_relative_path: str,
    *,
    project_root: Path | None = None,
) -> Path:
    """Resolve one project-local MP4 for the guarded preview endpoint."""

    pipeline, _project_dir, approved_root = _pipeline_context(
        session,
        pipeline_id,
        project_root=project_root,
    )
    path, relative = _resolve_project_file(
        pipeline,
        approved_root,
        artifact_relative_path,
        suffix=".mp4",
    )
    _artifact_sha, is_mp4 = _guarded_artifact_snapshot(pipeline, relative)
    if not is_mp4:
        raise QualityReportError("artifact is not a valid MP4")
    return path


def stream_quality_artifact(
    session: Session,
    pipeline_id: int,
    artifact_relative_path: str,
    *,
    project_root: Path | None = None,
    chunk_size: int = 1024 * 1024,
) -> tuple[str, Iterator[bytes]]:
    """Return a handle-guarded UX stream for one project-local MP4.

    ``FileResponse(path)`` reopens a pathname after validation, leaving a
    replace-between-check-and-open window.  This iterator opens one guarded
    handle and verifies path/handle identity before and after the bytes are
    yielded.  It is still a human-preview stream, not immutable release
    evidence; the quality/release paths re-hash independently.
    """

    if type(chunk_size) is not int or not 64 * 1024 <= chunk_size <= 8 * 1024 * 1024:
        raise QualityReportError("preview chunk size is invalid")
    pipeline, _project_dir, approved_root = _pipeline_context(
        session,
        pipeline_id,
        project_root=project_root,
    )
    path, relative = _resolve_project_file(
        pipeline,
        approved_root,
        artifact_relative_path,
        suffix=".mp4",
    )
    _artifact_sha, is_mp4 = _guarded_artifact_snapshot(pipeline, relative)
    if not is_mp4:
        raise QualityReportError("artifact is not a valid MP4")

    def iterator() -> Iterator[bytes]:
        try:
            project_use = begin_prepared_project_directory_use(
                pipeline,
                project_root=approved_root,
            )
            project_use.verify()
            directory_use = begin_prepared_project_child_directory_use(
                pipeline,
                project_root=approved_root,
                relative_path=Path(relative).parent,
                create=False,
            )
            expected = prepared_project_output_path(
                directory_use,
                Path(relative).name,
                must_exist=True,
            )
            before = expected.lstat()
            if not _safe_regular_snapshot(before):
                raise QualityReportError("preview artifact is not a safe regular file")
            with _open_guarded_read_handle(expected) as stream:
                immediate = os.fstat(stream.fileno())
                if (
                    not _safe_regular_snapshot(immediate)
                    or _cross_handle_snapshot(immediate)
                    != _cross_handle_snapshot(before)
                ):
                    raise QualityReportError("preview artifact changed before streaming")
                while True:
                    chunk = stream.read(chunk_size)
                    if not chunk:
                        break
                    yield chunk
                after_handle = os.fstat(stream.fileno())
                after_path = expected.lstat()
                if (
                    not _safe_regular_snapshot(after_handle)
                    or not _safe_regular_snapshot(after_path)
                    or _stat_snapshot(after_handle) != _stat_snapshot(immediate)
                    or _stat_snapshot(after_path) != _stat_snapshot(before)
                    or _cross_handle_snapshot(after_path)
                    != _cross_handle_snapshot(after_handle)
                ):
                    raise QualityReportError("preview artifact changed during streaming")
                directory_use.verify()
            project_use.verify()
        except QualityReportError:
            raise
        except (OSError, ProjectIdentityError, ValueError) as exc:
            raise QualityReportError("preview artifact could not be streamed safely") from exc

    return path.name, iterator()


def write_quality_report(
    session: Session,
    pipeline_id: int,
    artifact_relative_path: str,
    *,
    manual_gates: Mapping[str, Any] | None = None,
    quality_contract_review: Mapping[str, Any] | None = None,
    project_root: Path | None = None,
    ffprobe_executable: Path | str | None = None,
    ffmpeg_executable: Path | str | None = None,
    timeout_seconds: float = 30.0,
) -> QualityReportResult:
    """Probe one rendered MP4 and write one immutable quality report."""

    pipeline, project_dir, approved_root = _pipeline_context(
        session,
        pipeline_id,
        project_root=project_root,
    )
    artifact_path, artifact_relative = _resolve_project_file(
        pipeline,
        approved_root,
        artifact_relative_path,
        suffix=".mp4",
    )
    artifact_sha, _artifact_is_mp4 = _guarded_artifact_snapshot(
        pipeline,
        artifact_relative,
    )
    state_sha = pipeline_state_sha256(pipeline)

    timeline = _latest_timeline(pipeline, approved_root)
    _verify_current_artifact_lineage(
        pipeline,
        approved_root,
        artifact_relative=artifact_relative,
        artifact_sha256=artifact_sha,
        timeline=timeline,
    )
    structure_passed, structure_evidence = _structure_gate(
        timeline,
        artifact_relative=artifact_relative,
    )
    technical_report = _probe_artifact(
        artifact_path,
        timeline,
        ffprobe_executable=ffprobe_executable,
        ffmpeg_executable=ffmpeg_executable,
        timeout_seconds=timeout_seconds,
    )
    # The probe operates on a pathname, while the first hash was taken from a
    # guarded handle.  Re-read through the same guard so a concurrent file
    # replacement cannot make technical evidence refer to different bytes.
    post_probe_sha, _ = _guarded_artifact_snapshot(
        pipeline,
        artifact_relative,
    )
    if post_probe_sha != artifact_sha:
        raise QualityReportError("artifact changed during technical probing")
    technical_passed, technical_evidence = _technical_gate(technical_report)
    gates = {
        "structure": {
            "status": "passed" if structure_passed else "failed",
            "evidence": structure_evidence,
        },
        "technical": {
            "status": "passed" if technical_passed else "failed",
            "evidence": technical_evidence,
        },
    }
    for name in ("visual_narration", "facts_rights"):
        gates[name] = _manual_gate(manual_gates, name)
    try:
        gates["quality_contract"] = evaluate_quality_contract_review(
            session,
            pipeline_id,
            quality_contract_review,
            project_root=approved_root,
        )
    except QualityContractError as exc:
        raise QualityReportError(str(exc)) from exc
    blockers = [name for name in _GATE_NAMES if gates[name]["status"] != "passed"]
    generated_at = datetime.now(timezone.utc).isoformat()
    report = {
        "report_format": REPORT_FORMAT,
        "pipeline_id": int(pipeline.id),
        "pipeline_state_sha256": state_sha,
        "artifact_sha256": artifact_sha,
        "generated_at": generated_at,
        "gates": gates,
        "release_blockers": blockers,
    }
    encoded = _canonical_json(report)
    if len(encoded) > _MAX_REPORT_BYTES:
        raise QualityReportError("quality report exceeds the guarded size limit")
    _report_path, report_relative = _write_report(
        pipeline,
        approved_root,
        encoded,
    )
    # Re-read the committed report bytes so the hash in the API is exactly the
    # bytes that the completion policy will later verify.
    report_bytes = _guarded_report_snapshot(pipeline, report_relative)
    report_sha = hashlib.sha256(report_bytes).hexdigest()
    return QualityReportResult(
        pipeline_id=int(pipeline.id),
        artifact_relative_path=artifact_relative,
        artifact_sha256=artifact_sha,
        report_relative_path=report_relative,
        report_sha256=report_sha,
        report=report,
    )


def approve_quality_report(
    session: Session,
    pipeline_id: int,
    *,
    artifact_relative_path: str,
    quality_report_relative_path: str,
    approved_by: str,
    approved_at: str | None = None,
    project_root: Path | None = None,
) -> QualityApprovalResult:
    """Verify a report and atomically write completion evidence for Ian."""

    if approved_by != "Ian":
        raise QualityReportError("only Ian can approve a production artifact")
    pipeline, _project_dir, approved_root = _pipeline_context(
        session,
        pipeline_id,
        project_root=project_root,
    )
    artifact_path, artifact_relative = _resolve_project_file(
        pipeline,
        approved_root,
        artifact_relative_path,
        suffix=".mp4",
    )
    report_path, report_relative = _resolve_project_file(
        pipeline,
        approved_root,
        quality_report_relative_path,
        suffix=".json",
    )
    artifact_sha, artifact_is_mp4 = _guarded_artifact_snapshot(
        pipeline,
        artifact_relative,
    )
    report_bytes = _guarded_report_snapshot(pipeline, report_relative)
    report = _parse_strict_json(report_bytes)
    _validate_report_shape(report)
    if _parse_quality_contract_gate_evidence(
        report["gates"]["quality_contract"]["evidence"]
    ) is None:
        raise QualityReportError(
            "quality contract requires a complete structured review before approval"
        )
    report_sha = hashlib.sha256(report_bytes).hexdigest()
    state_sha = pipeline_state_sha256(pipeline)
    _verify_current_artifact_lineage(
        pipeline,
        approved_root,
        artifact_relative=artifact_relative,
        artifact_sha256=artifact_sha,
        timeline=_latest_timeline(pipeline, approved_root),
    )
    if (
        report["pipeline_id"] != pipeline.id
        or report["pipeline_state_sha256"] != state_sha
        or not artifact_is_mp4
        or report["artifact_sha256"] != artifact_sha
        or any(gate["status"] != "passed" for gate in report["gates"].values())
        or report["release_blockers"]
    ):
        raise QualityReportError("quality report is not currently releasable")
    approval_time = approved_at or datetime.now(timezone.utc).isoformat()
    evidence = {
        "evidence_version": 2,
        "pipeline_state_sha256": state_sha,
        "artifact_relative_path": artifact_relative,
        "artifact_sha256": artifact_sha,
        "quality_report_relative_path": report_relative,
        "quality_report_sha256": report_sha,
        "approved_at": approval_time,
        "approved_by": "Ian",
    }
    previous = deepcopy(getattr(pipeline, "completion_evidence", {}) or {})
    expected_updated_at = getattr(pipeline, "updated_at", None)
    pipeline.completion_evidence = deepcopy(evidence)
    decision = evaluate_pipeline_completion(pipeline)
    if not decision.is_complete:
        pipeline.completion_evidence = previous
        raise QualityReportError(
            f"pipeline is not ready for approval: {decision.reason_code}"
        )
    # The JSON column is used as a whole-document compare-and-swap so a stale
    # approval cannot overwrite a newer approval or revocation.
    try:
        try:
            with session.no_autoflush:
                statement = update(ProductionPipeline).where(
                    ProductionPipeline.id == pipeline.id,
                    ProductionPipeline.completion_evidence == previous,
                )
                if expected_updated_at is not None:
                    statement = statement.where(
                        ProductionPipeline.updated_at == expected_updated_at
                    )
                result = session.execute(
                    statement.values(completion_evidence=deepcopy(evidence))
                )
        except BaseException as exc:
            # An execute/flush acknowledgement can be lost just like commit.
            # The UPDATE may have reached SQLite/the remote DB, so callers must
            # reconcile rather than retry a possibly duplicated approval.
            try:
                session.rollback()
            except Exception:
                pass
            raise QualityApprovalCommitUnknownError(
                "approval write acknowledgement is unknown; reconcile before retrying"
            ) from exc
        if int(result.rowcount or 0) != 1:
            session.rollback()
            raise QualityApprovalConflictError(
                "completion evidence changed; reload the pipeline and retry"
            )
        try:
            session.commit()
        except BaseException as exc:
            try:
                session.rollback()
            except Exception:
                pass
            raise QualityApprovalCommitUnknownError(
                "approval commit acknowledgement is unknown; reconcile before retrying"
            ) from exc
        # A successful commit is not enough to claim a safe approval: the
        # session must be re-read and the exact completion decision verified.
        # If that reconciliation fails, the evidence may already be durable;
        # never turn the situation into an ordinary 400/retry instruction.
        try:
            session.expire_all()
            refreshed = session.get(ProductionPipeline, pipeline.id)
            if refreshed is None:
                raise QualityApprovalReconcileError(
                    "approval committed but the pipeline could not be re-read; reconcile before retrying"
                )
            post_decision = evaluate_pipeline_completion(refreshed)
            if not post_decision.is_complete:
                raise QualityApprovalReconcileError(
                    "approval committed but fresh completion evidence is no longer complete; reconcile before retrying"
                )
        except QualityApprovalReconcileError:
            raise
        except BaseException as exc:
            raise QualityApprovalReconcileError(
                "approval committed but fresh verification failed; reconcile before retrying"
            ) from exc
    finally:
        # Restore the session identity map to the committed database value even
        # when SQLAlchemy expires only selected JSON attributes.
        try:
            session.expire(pipeline, ["completion_evidence"])
            session.refresh(pipeline)
        except Exception:
            pass
    return QualityApprovalResult(
        pipeline_id=int(pipeline.id),
        evidence=evidence,
        decision={
            "is_complete": decision.is_complete,
            "status": decision.status,
            "reason_code": decision.reason_code,
            "message": decision.message,
            "next_action": decision.next_action,
        },
    )


def _pipeline_context(
    session: Session,
    pipeline_id: int,
    *,
    project_root: Path | None,
) -> tuple[ProductionPipeline, Path, Path]:
    pipeline = session.get(ProductionPipeline, pipeline_id)
    if pipeline is None:
        raise QualityReportError(f"pipeline not found: {pipeline_id}")
    if not pipeline.project_dir:
        raise QualityReportError("pipeline project directory is missing")
    approved_root = resolve_configured_path(project_root or get_settings().project_root_dir)
    try:
        project_use = begin_prepared_project_directory_use(
            pipeline,
            project_root=approved_root,
        )
        project_use.verify()
    except (ProjectIdentityError, OSError, ValueError) as exc:
        raise QualityReportError(f"project identity check failed: {exc}") from exc
    return pipeline, project_use.path, approved_root


def _resolve_project_file(
    pipeline: ProductionPipeline,
    approved_root: Path,
    raw: str,
    *,
    suffix: str,
) -> tuple[Path, str]:
    value = str(raw or "").strip()
    pattern = _RELATIVE_MP4 if suffix == ".mp4" else _RELATIVE_JSON
    if (
        not pattern.fullmatch(value)
        or "\\" in value
        or ":" in value
        or "//" in value
    ):
        raise QualityReportError("artifact path must be a project-relative 成片 path")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise QualityReportError("artifact path contains an unsafe component")
    try:
        directory_use = begin_prepared_project_child_directory_use(
            pipeline,
            project_root=approved_root,
            relative_path=Path(*parts[:-1]),
            create=False,
        )
        path = prepared_project_output_path(
            directory_use,
            parts[-1],
            must_exist=True,
        )
        directory_use.verify()
    except (ProjectIdentityError, OSError, ValueError) as exc:
        raise QualityReportError("artifact path is missing or unsafe") from exc
    if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
        raise QualityReportError("artifact path is not a plain regular file")
    return path, PurePosixPath(*parts).as_posix()


def _latest_timeline(
    pipeline: ProductionPipeline,
    approved_root: Path,
):
    try:
        latest = TimelineSpecLedger(pipeline, project_root=approved_root).latest()
    except (TimelineSpecError, ProjectIdentityError, OSError, ValueError) as exc:
        raise QualityReportError("timeline spec cannot be read") from exc
    return latest


def _verify_current_artifact_lineage(
    pipeline: ProductionPipeline,
    approved_root: Path,
    *,
    artifact_relative: str,
    artifact_sha256: str,
    timeline: Any,
) -> None:
    """Prove that an MP4 was rendered from the current TimelineSpec."""

    timeline_version_id = str(getattr(timeline, "version_id", "") or "").strip()
    timeline_input_hash = str(getattr(timeline, "input_hash", "") or "").casefold()
    if not timeline_version_id or not _is_sha256(timeline_input_hash):
        raise QualityReportError(
            "artifact_timeline_mismatch: current TimelineSpec lineage is missing"
        )
    rendered_version_id, rendered_input_hash = _verified_artifact_lineage(
        pipeline,
        approved_root,
        artifact_relative=artifact_relative,
        artifact_sha256=artifact_sha256,
    )
    if (
        rendered_version_id != timeline_version_id
        or rendered_input_hash != timeline_input_hash
    ):
        raise QualityReportError(
            "artifact_timeline_mismatch: rendered artifact does not belong to the current TimelineSpec"
        )


def _verified_artifact_lineage(
    pipeline: ProductionPipeline,
    approved_root: Path,
    *,
    artifact_relative: str,
    artifact_sha256: str,
) -> tuple[str, str]:
    """Return immutable renderer lineage only after all sibling hashes verify."""

    parent = PurePosixPath(artifact_relative).parent
    technical_relative = PurePosixPath(parent, "technical-report.json").as_posix()
    try:
        _technical_path, technical_relative = _resolve_project_file(
            pipeline,
            approved_root,
            technical_relative,
            suffix=".json",
        )
        technical_bytes = _guarded_report_snapshot(pipeline, technical_relative)
        technical = _parse_strict_json(technical_bytes)
    except QualityReportError as exc:
        raise QualityReportError(
            "artifact_lineage_invalid: technical render report is missing or invalid"
        ) from exc
    technical_artifact_sha = str(technical.get("artifact_sha256") or "").casefold()
    technical_timeline_hash = str(technical.get("timeline_input_hash") or "").casefold()
    technical_version_id = str(technical.get("timeline_version_id") or "").strip()
    visual_manifest_sha = str(
        technical.get("visual_render_manifest_sha256") or ""
    ).casefold()
    if (
        technical.get("schema_version") != 1
        or not technical_version_id
        or not _is_sha256(technical_timeline_hash)
        or technical_artifact_sha != artifact_sha256
        or not _is_sha256(visual_manifest_sha)
    ):
        raise QualityReportError(
            "artifact_lineage_invalid: technical render lineage is invalid"
        )
    visual_relative = PurePosixPath(
        parent,
        "visual-root",
        technical_version_id,
        "render-manifest.json",
    ).as_posix()
    try:
        visual_path, _ = _resolve_project_file(
            pipeline,
            approved_root,
            visual_relative,
            suffix=".json",
        )
        actual_visual_sha = _stable_file_sha256(
            visual_path,
            label="visual render manifest",
        )
    except QualityReportError:
        raise
    except OSError as exc:
        raise QualityReportError(
            "artifact_lineage_invalid: visual render manifest is unreadable"
        ) from exc
    if actual_visual_sha != visual_manifest_sha:
        raise QualityReportError(
            "artifact_lineage_invalid: visual render manifest hash mismatch"
        )
    return technical_version_id, technical_timeline_hash


def _stable_file_sha256(path: Path, *, label: str) -> str:
    before = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    after = path.stat()
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise QualityReportError(f"artifact_lineage_invalid: {label} changed while reading")
    return digest.hexdigest()


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value.casefold())
    )


def _structure_gate(
    timeline: Any,
    *,
    artifact_relative: str,
) -> tuple[bool, list[str]]:
    evidence: list[str] = [f"artifact={artifact_relative}"]
    if timeline is None:
        return False, evidence + ["timeline_spec_missing"]
    artifacts = getattr(timeline, "artifacts", {})
    voiceover = artifacts.get("voiceover") if isinstance(artifacts, Mapping) else None
    subtitles = artifacts.get("subtitles") if isinstance(artifacts, Mapping) else None
    passed = bool(timeline.segments and isinstance(voiceover, Mapping) and isinstance(subtitles, Mapping))
    evidence.append(f"segments={len(getattr(timeline, 'segments', ()))}")
    evidence.append("timeline_media_refs=" + ("present" if passed else "missing"))
    return passed, evidence


def _probe_artifact(
    path: Path,
    timeline: Any,
    *,
    ffprobe_executable: Path | str | None,
    ffmpeg_executable: Path | str | None,
    timeout_seconds: float,
) -> VideoTechnicalReport | None:
    settings = getattr(timeline, "settings", {}) if timeline is not None else {}
    try:
        width = int(settings.get("width", 1080))
        height = int(settings.get("height", 1920))
        fps = float(settings.get("fps", 30))
    except (TypeError, ValueError):
        width, height, fps = 1080, 1920, 30.0
    try:
        return probe_video(
            path,
            expected=VideoTechnicalExpectation(
                width=width,
                height=height,
                fps=fps,
                require_audio=True,
                require_subtitles=True,
            ),
            ffprobe_executable=ffprobe_executable,
            ffmpeg_executable=ffmpeg_executable,
            timeout_seconds=timeout_seconds,
        )
    except (VideoTechnicalProbeError, OSError, ValueError) as exc:
        return None


def _technical_gate(report: VideoTechnicalReport | None) -> tuple[bool, list[str]]:
    if report is None:
        return False, ["technical_probe_unavailable"]
    return report.is_valid, [
        f"probe_valid={str(report.is_valid).lower()}",
        f"dimensions={report.width}x{report.height}",
        f"fps={report.fps}",
        f"audio_streams={report.audio_streams}",
        f"subtitle_streams={report.subtitle_streams}",
    ]


def _guarded_artifact_snapshot(
    pipeline: ProductionPipeline,
    relative: str,
) -> tuple[str, bool]:
    try:
        return _read_with_guard(
            pipeline,
            Path(*relative.split("/")),
            system_drive=None,
            reader=_read_artifact,
        )
    except (_GuardedFileMissing, _GuardedFileUnsafe, _GuardedFileTooLarge) as exc:
        raise QualityReportError("artifact could not be read through the guarded project boundary") from exc


def _guarded_report_snapshot(
    pipeline: ProductionPipeline,
    relative: str,
) -> bytes:
    try:
        return _read_with_guard(
            pipeline,
            Path(*relative.split("/")),
            system_drive=None,
            reader=_read_report,
        )
    except (_GuardedFileMissing, _GuardedFileUnsafe, _GuardedFileTooLarge) as exc:
        raise QualityReportError("quality report could not be read through the guarded project boundary") from exc


def _parse_strict_json(content: bytes) -> dict[str, Any]:
    if content.startswith(b"\xef\xbb\xbf"):
        raise QualityReportError("quality report must not contain a BOM")

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON constant: {value}")

    def reject_duplicate(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    try:
        payload = json.loads(
            content.decode("utf-8", errors="strict"),
            object_pairs_hook=reject_duplicate,
            parse_constant=reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError, RecursionError) as exc:
        raise QualityReportError("quality report JSON is invalid") from exc
    if not isinstance(payload, dict):
        raise QualityReportError("quality report JSON root must be an object")
    if not _is_strict_json(payload):
        raise QualityReportError("quality report JSON exceeds safety limits")
    return payload


def _manual_gate(manual_gates: Mapping[str, Any] | None, name: str) -> dict[str, Any]:
    raw = manual_gates.get(name) if isinstance(manual_gates, Mapping) else None
    if not isinstance(raw, Mapping):
        return {"status": "failed", "evidence": [f"manual_review_required:{name}"]}
    status = raw.get("status")
    if status not in {"passed", "failed"}:
        raise QualityReportError(f"manual gate {name} status is invalid")
    evidence = raw.get("evidence")
    if isinstance(evidence, str):
        evidence = [evidence]
    if (
        not isinstance(evidence, list)
        or not evidence
        or len(evidence) > _MAX_MANUAL_EVIDENCE_ITEMS
        or any(not isinstance(item, str) or not item.strip() for item in evidence)
        or any(len(item.strip()) > _MAX_MANUAL_EVIDENCE_LENGTH for item in evidence)
    ):
        raise QualityReportError(f"manual gate {name} evidence is required")
    return {
        "status": status,
        "evidence": [item.strip() for item in evidence],
    }


def _validate_report_shape(report: Any) -> None:
    if not isinstance(report, Mapping) or set(report) != {
        "report_format",
        "pipeline_id",
        "pipeline_state_sha256",
        "artifact_sha256",
        "generated_at",
        "gates",
        "release_blockers",
    }:
        raise QualityReportError("quality report schema is invalid")
    if report.get("report_format") != REPORT_FORMAT:
        raise QualityReportError("quality report format is invalid")
    if type(report.get("pipeline_id")) is not int or report["pipeline_id"] <= 0:
        raise QualityReportError("quality report pipeline id is invalid")
    for key in ("pipeline_state_sha256", "artifact_sha256"):
        value = report.get(key)
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
            raise QualityReportError("quality report hash is invalid")
    if not isinstance(report.get("generated_at"), str):
        raise QualityReportError("quality report timestamp is invalid")
    gates = report.get("gates")
    if not isinstance(gates, Mapping) or set(gates) != set(_GATE_NAMES):
        raise QualityReportError("quality report gates are invalid")
    for name in _GATE_NAMES:
        gate = gates[name]
        if (
            not isinstance(gate, Mapping)
            or set(gate) != {"status", "evidence"}
            or gate.get("status") not in {"passed", "failed"}
            or not isinstance(gate.get("evidence"), list)
            or not gate["evidence"]
            or any(
                not isinstance(item, str)
                or not item
                or item != item.strip()
                for item in gate["evidence"]
            )
        ):
            raise QualityReportError(f"quality report gate {name} is invalid")
    blockers = report.get("release_blockers")
    expected_blockers = [
        name for name in _GATE_NAMES if gates[name]["status"] != "passed"
    ]
    if blockers != expected_blockers:
        raise QualityReportError("quality report release blockers are invalid")


def _write_report(
    pipeline: ProductionPipeline,
    approved_root: Path,
    encoded: bytes,
) -> tuple[Path, str]:
    for _attempt in range(4):
        filename = f"qr-{uuid4().hex}.json"
        try:
            directory_use = begin_prepared_project_child_directory_use(
                pipeline,
                project_root=approved_root,
                relative_path=_REPORT_DIR,
                create=True,
            )
            report_path = prepared_project_output_path(
                directory_use,
                filename,
                must_exist=False,
            )
            try:
                with report_path.open("xb") as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
            except FileExistsError:
                directory_use.verify()
                continue
            directory_use.verify()
            relative = PurePosixPath(_REPORT_DIR.as_posix(), filename).as_posix()
            return report_path, relative
        except (ProjectIdentityError, OSError, ValueError) as exc:
            raise QualityReportError("quality report could not be written") from exc
    raise QualityReportError("quality report filename collision; retry safely")


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
        "utf-8"
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "QualityApprovalCommitUnknownError",
    "QualityApprovalConflictError",
    "QualityApprovalReconcileError",
    "QualityApprovalResult",
    "QualityArtifactCandidate",
    "QualityReportError",
    "QualityReportResult",
    "approve_quality_report",
    "list_quality_artifact_candidates",
    "resolve_quality_artifact_path",
    "stream_quality_artifact",
    "write_quality_report",
]
