"""Create an immutable, project-local release package after approval.

The legacy finished-video exporter is intentionally not used here.  It writes
to the historical archive chain and predates the five-gate completion contract.
This adapter consumes only a pipeline's verified completion evidence, copies
the approved MP4 into the prepared D: project, and writes a hash-bound manifest
that later publishing integrations can consume.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
from typing import Any, Mapping
from uuid import uuid4

from sqlalchemy.orm import Session

from app.core.config import get_settings, resolve_configured_path
from app.materials.stock_rights_evidence import (
    StockRightsEvidenceError,
    normalise_stock_rights_evidence,
)
from app.models import ProductionPipeline
from app.pipeline.completion_policy import (
    _GuardedFileMissing,
    _GuardedFileTooLarge,
    _GuardedFileUnsafe,
    _read_artifact,
    _read_report,
    _read_with_guard,
    _validated_relative_path,
    evaluate_pipeline_completion,
)
from app.pipeline.project_identity import (
    ProjectIdentityError,
    begin_prepared_project_child_directory_use,
    begin_prepared_project_directory_use,
    prepared_project_output_path,
)


RELEASE_FORMAT = "auto-video-engine-release-package-v1"
_RELEASE_DIR = Path("成片") / "releases"
_MAX_MANIFEST_BYTES = 2 * 1024 * 1024


class ReleasePackageError(ValueError):
    """A release package request failed closed."""


class ReleasePackageBlockedError(ReleasePackageError):
    """The canonical completion evaluator did not approve the pipeline."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(f"release blocked: {reason_code}: {message}")
        self.reason_code = reason_code
        self.message = message


class ReleasePackageConflictError(ReleasePackageError):
    """An existing release artifact or manifest does not match its hash."""


class ReleasePackageCommitUnknownError(RuntimeError):
    """A local rename/write acknowledgement was lost; reconcile before retry."""


@dataclass(frozen=True, slots=True)
class ReleasePackageResult:
    pipeline_id: int
    source_artifact_relative_path: str
    artifact_relative_path: str
    artifact_sha256: str
    source_quality_report_relative_path: str
    quality_report_relative_path: str
    quality_report_sha256: str
    release_manifest_relative_path: str
    release_manifest_sha256: str
    released_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "pipeline_id": self.pipeline_id,
            "release_format": RELEASE_FORMAT,
            "source_artifact_relative_path": self.source_artifact_relative_path,
            "artifact_relative_path": self.artifact_relative_path,
            "artifact_sha256": self.artifact_sha256,
            "source_quality_report_relative_path": self.source_quality_report_relative_path,
            "quality_report_relative_path": self.quality_report_relative_path,
            "quality_report_sha256": self.quality_report_sha256,
            "release_manifest_relative_path": self.release_manifest_relative_path,
            "release_manifest_sha256": self.release_manifest_sha256,
            "released_at": self.released_at,
            "release_supported": True,
            "external_publish_supported": False,
        }


def create_release_package(
    session: Session,
    pipeline_id: int,
    *,
    project_root: Path | None = None,
) -> ReleasePackageResult:
    """Promote the already-approved artifact into a local release package.

    This function is deliberately idempotent for the same artifact hash.  It
    never changes pipeline status or completion evidence and never publishes to
    an external platform.
    """

    pipeline = session.get(
        ProductionPipeline,
        pipeline_id,
        populate_existing=True,
    )
    if pipeline is None:
        raise ReleasePackageError(f"pipeline not found: {pipeline_id}")
    if not pipeline.project_dir:
        raise ReleasePackageError("pipeline project directory is missing")
    decision = evaluate_pipeline_completion(pipeline)
    if not decision.is_complete:
        raise ReleasePackageBlockedError(decision.reason_code, decision.message)
    evidence = getattr(pipeline, "completion_evidence", None)
    if not isinstance(evidence, dict):
        raise ReleasePackageBlockedError("approval_missing", "completion evidence is missing")
    expected_evidence = dict(evidence)

    source_relative_path = _validated_relative_path(
        evidence.get("artifact_relative_path"), suffix=".mp4"
    )
    report_relative_path = _validated_relative_path(
        evidence.get("quality_report_relative_path"), suffix=".json"
    )
    if source_relative_path is None or report_relative_path is None:
        raise ReleasePackageError("completion evidence paths are invalid")
    source_relative = source_relative_path.as_posix()
    report_relative = report_relative_path.as_posix()
    expected_artifact_sha = evidence.get("artifact_sha256")
    expected_report_sha = evidence.get("quality_report_sha256")
    if not _is_sha256(expected_artifact_sha) or not _is_sha256(expected_report_sha):
        raise ReleasePackageError("completion evidence hashes are invalid")

    approved_root = resolve_configured_path(project_root or get_settings().project_root_dir)
    try:
        project_use = begin_prepared_project_directory_use(
            pipeline,
            project_root=approved_root,
        )
        project_use.verify()
    except (ProjectIdentityError, OSError, ValueError) as exc:
        raise ReleasePackageError(f"project identity check failed: {exc}") from exc

    artifact_sha, artifact_is_mp4 = _guarded_artifact_snapshot(
        pipeline, source_relative_path
    )
    if not artifact_is_mp4 or artifact_sha != expected_artifact_sha:
        raise ReleasePackageBlockedError(
            "artifact_hash_mismatch",
            "approved artifact changed before release package creation",
        )
    report_bytes = _guarded_report_snapshot(pipeline, report_relative_path)
    report_sha = hashlib.sha256(report_bytes).hexdigest()
    if report_sha != expected_report_sha:
        raise ReleasePackageBlockedError(
            "quality_report_hash_mismatch",
            "approved quality report changed before release package creation",
        )

    quality_report = _decode_quality_report(report_bytes)
    release_metadata = _build_release_metadata(pipeline, quality_report)

    state_sha = evidence.get("pipeline_state_sha256")
    if not _is_sha256(state_sha):
        raise ReleasePackageError("completion evidence state hash is invalid")
    token = f"{str(expected_artifact_sha)[:16]}-{str(expected_report_sha)[:8]}-{str(state_sha)[:8]}"
    artifact_name = f"release-{token}.mp4"
    release_report_name = f"release-{token}.quality.json"
    manifest_name = f"release-{token}.json"
    artifact_relative = PurePosixPath(_RELEASE_DIR.as_posix(), artifact_name).as_posix()
    release_report_relative = PurePosixPath(
        _RELEASE_DIR.as_posix(), release_report_name
    ).as_posix()
    manifest_relative = PurePosixPath(_RELEASE_DIR.as_posix(), manifest_name).as_posix()
    released_at = datetime.now(timezone.utc).isoformat()

    try:
        release_dir_use = begin_prepared_project_child_directory_use(
            pipeline,
            project_root=approved_root,
            relative_path=_RELEASE_DIR,
            create=True,
        )
        target_path = prepared_project_output_path(
            release_dir_use,
            artifact_name,
            must_exist=False,
        )
        manifest_path = prepared_project_output_path(
            release_dir_use,
            manifest_name,
            must_exist=False,
        )
        release_report_path = prepared_project_output_path(
            release_dir_use,
            release_report_name,
            must_exist=False,
        )
        if manifest_path.exists():
            released_at = _existing_release_time(
                pipeline,
                manifest_relative,
                expected_artifact_sha=expected_artifact_sha,
                expected_report_sha=expected_report_sha,
                artifact_relative=artifact_relative,
                source_relative=source_relative,
                release_report_relative=release_report_relative,
            )
        manifest = {
            "release_format": RELEASE_FORMAT,
            "pipeline_id": int(pipeline.id),
            "source_artifact_relative_path": source_relative,
            "artifact_relative_path": artifact_relative,
            "artifact_sha256": expected_artifact_sha,
            "source_quality_report_relative_path": report_relative,
            "quality_report_relative_path": release_report_relative,
            "quality_report_sha256": expected_report_sha,
            "pipeline_state_sha256": state_sha,
            "approved_by": evidence.get("approved_by"),
            "approved_at": evidence.get("approved_at"),
            "released_at": released_at,
            "provenance": {
                "pipeline_state_sha256": state_sha,
                "source_artifact_relative_path": source_relative,
                "source_quality_report_relative_path": report_relative,
                "approved_by": evidence.get("approved_by"),
                "approved_at": evidence.get("approved_at"),
            },
            "publication_metadata": release_metadata["publication_metadata"],
            "cover": release_metadata["cover"],
            "fact_sources": release_metadata["fact_sources"],
            "materials": release_metadata["materials"],
            "rights_review": release_metadata["rights_review"],
            "ai_disclosure": release_metadata["ai_disclosure"],
            "platform_checklist": release_metadata["platform_checklist"],
        }
        encoded_manifest = _canonical_json(manifest)
        if len(encoded_manifest) > _MAX_MANIFEST_BYTES:
            raise ReleasePackageError("release manifest exceeds the guarded size limit")
        _ensure_release_artifact(
            pipeline,
            source_relative_path,
            artifact_relative,
            target_path,
            expected_artifact_sha,
            release_dir_use,
        )
        _ensure_release_bytes(
            pipeline,
            release_report_relative,
            release_report_path,
            report_bytes,
            release_dir_use,
        )
        _ensure_release_manifest(
            pipeline,
            manifest_relative,
            manifest_path,
            encoded_manifest,
            release_dir_use,
        )
        release_dir_use.verify()
    except ReleasePackageError:
        raise
    except (ProjectIdentityError, OSError, ValueError) as exc:
        raise ReleasePackageError("release package could not be written") from exc

    # A package write is a side effect.  Reconcile the upstream evidence and
    # both source files after it completes; if anything moved during the copy,
    # do not report a clean success that a caller could publish blindly.
    try:
        final_source_sha, final_source_is_mp4 = _guarded_artifact_snapshot(
            pipeline, source_relative_path
        )
        final_report_bytes = _guarded_report_snapshot(pipeline, report_relative_path)
        if (
            not final_source_is_mp4
            or final_source_sha != expected_artifact_sha
            or hashlib.sha256(final_report_bytes).hexdigest() != expected_report_sha
        ):
            raise ReleasePackageCommitUnknownError(
                "release package written but approved source/report changed; reconcile before retry"
            )
        refreshed = session.get(
            ProductionPipeline,
            pipeline.id,
            populate_existing=True,
        )
        if refreshed is None:
            raise ReleasePackageCommitUnknownError(
                "release package written but pipeline disappeared; reconcile before retry"
            )
        refreshed_decision = evaluate_pipeline_completion(refreshed)
        refreshed_evidence = getattr(refreshed, "completion_evidence", None)
        evidence_keys = (
            "evidence_version",
            "pipeline_state_sha256",
            "artifact_relative_path",
            "artifact_sha256",
            "quality_report_relative_path",
            "quality_report_sha256",
            "approved_at",
            "approved_by",
        )
        if (
            not refreshed_decision.is_complete
            or not isinstance(refreshed_evidence, dict)
            or any(
                refreshed_evidence.get(key) != expected_evidence.get(key)
                for key in evidence_keys
            )
        ):
            raise ReleasePackageCommitUnknownError(
                "release package written but completion evidence changed; reconcile before retry"
            )
    except ReleasePackageCommitUnknownError:
        raise
    except Exception as exc:
        raise ReleasePackageCommitUnknownError(
            "release package written but reconciliation failed; inspect the package before retry"
        ) from exc

    # The package may already exist by the time any of these reads fail.  A
    # post-side-effect failure is therefore always a reconcile/503 condition,
    # never an ordinary conflict that invites a blind retry.
    try:
        final_artifact_sha, final_artifact_is_mp4 = _guarded_artifact_snapshot(
            pipeline, Path(*artifact_relative.split("/"))
        )
        if not final_artifact_is_mp4 or final_artifact_sha != expected_artifact_sha:
            raise ReleasePackageCommitUnknownError(
                "release package written but release artifact hash could not be verified; reconcile before retry"
            )
        final_release_report = _guarded_report_snapshot(pipeline, release_report_relative)
        if hashlib.sha256(final_release_report).hexdigest() != expected_report_sha:
            raise ReleasePackageCommitUnknownError(
                "release package written but copied quality report hash could not be verified; reconcile before retry"
            )
        final_manifest = _guarded_report_snapshot(pipeline, manifest_relative)
        final_manifest_sha = hashlib.sha256(final_manifest).hexdigest()
        if final_manifest != encoded_manifest:
            raise ReleasePackageCommitUnknownError(
                "release package written but manifest changed; reconcile before retry"
            )
    except ReleasePackageCommitUnknownError:
        raise
    except Exception as exc:
        raise ReleasePackageCommitUnknownError(
            "release package written but final package verification failed; reconcile before retry"
        ) from exc
    return ReleasePackageResult(
        pipeline_id=int(pipeline.id),
        source_artifact_relative_path=source_relative,
        artifact_relative_path=artifact_relative,
        artifact_sha256=final_artifact_sha,
        source_quality_report_relative_path=report_relative,
        quality_report_relative_path=release_report_relative,
        quality_report_sha256=expected_report_sha,
        release_manifest_relative_path=manifest_relative,
        release_manifest_sha256=final_manifest_sha,
        released_at=released_at,
    )


def _ensure_release_artifact(
    pipeline: ProductionPipeline,
    source_relative: Path,
    target_relative: str,
    target_path: Path,
    expected_sha: str,
    directory_use: Any,
) -> None:
    if target_path.exists():
        actual, valid = _guarded_artifact_snapshot(
            pipeline, Path(*target_relative.split("/"))
        )
        if valid and actual == expected_sha:
            return
        raise ReleasePackageConflictError("existing release artifact does not match approved hash")

    temporary = target_path.with_name(f".{target_path.name}.{uuid4().hex}.part")
    try:
        def copy_reader(stream: Any) -> str:
            stream.seek(0)
            digest = hashlib.sha256()
            with temporary.open("xb") as output:
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            return digest.hexdigest()

        copied_sha = _read_with_guard(
            pipeline,
            source_relative,
            system_drive=None,
            reader=copy_reader,
        )
        if copied_sha != expected_sha:
            raise ReleasePackageConflictError("source artifact changed during release copy")
        directory_use.verify()
        try:
            _publish_no_replace(temporary, target_path)
        except FileExistsError:
            actual, valid = _guarded_artifact_snapshot(
                pipeline, Path(*target_relative.split("/"))
            )
            if not valid or actual != expected_sha:
                raise ReleasePackageConflictError(
                    "release artifact appeared concurrently with a different hash"
                )
        except OSError as exc:
            raise ReleasePackageCommitUnknownError(
                "release artifact rename acknowledgement is unknown; reconcile before retry"
            ) from exc
        directory_use.verify()
    finally:
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError:
            pass


def _ensure_release_manifest(
    pipeline: ProductionPipeline,
    relative: str,
    path: Path,
    content: bytes,
    directory_use: Any,
) -> None:
    if path.exists():
        existing = _guarded_report_snapshot(pipeline, relative)
        if existing != content:
            raise ReleasePackageConflictError("existing release manifest does not match package")
        return
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.part")
    try:
        with temporary.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        directory_use.verify()
        try:
            _publish_no_replace(temporary, path)
        except FileExistsError:
            existing = _guarded_report_snapshot(pipeline, relative)
            if existing != content:
                raise ReleasePackageConflictError(
                    "release manifest appeared concurrently with different content"
                )
        except OSError as exc:
            raise ReleasePackageCommitUnknownError(
                "release manifest rename acknowledgement is unknown; reconcile before retry"
            ) from exc
        directory_use.verify()
    finally:
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError:
            pass


def _ensure_release_bytes(
    pipeline: ProductionPipeline,
    relative: str,
    path: Path,
    content: bytes,
    directory_use: Any,
) -> None:
    """Write one immutable release-sidecar file with no-replace semantics."""

    if path.exists():
        existing = _guarded_report_snapshot(pipeline, relative)
        if existing != content:
            raise ReleasePackageConflictError("existing release sidecar does not match package")
        return
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.part")
    try:
        with temporary.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        directory_use.verify()
        try:
            _publish_no_replace(temporary, path)
        except FileExistsError:
            existing = _guarded_report_snapshot(pipeline, relative)
            if existing != content:
                raise ReleasePackageConflictError(
                    "release sidecar appeared concurrently with different content"
                )
        except OSError as exc:
            raise ReleasePackageCommitUnknownError(
                "release sidecar publish acknowledgement is unknown; reconcile before retry"
            ) from exc
        directory_use.verify()
    finally:
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError:
            pass


def _decode_quality_report(content: bytes) -> dict[str, Any]:
    """Decode report metadata without weakening the hash-bound release gate."""

    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _build_release_metadata(
    pipeline: ProductionPipeline,
    quality_report: Mapping[str, Any],
) -> dict[str, Any]:
    """Build an honest, bounded publishing handoff manifest.

    The local package records what the current project actually knows.  It
    never invents a licence or fact source: legacy projects are explicitly
    marked as unavailable, while a candidate pool with a locked unknown-rights
    item blocks packaging.
    """

    script = getattr(pipeline, "script_asset", None)
    publication_metadata = {
        "title_options": _bounded_list(
            getattr(script, "title_options", None), max_items=12, max_chars=500
        ),
        "cover_copy": _bounded_list(
            getattr(script, "cover_copy", None), max_items=12, max_chars=500
        ),
        "publish_copy": _bounded_mapping(
            getattr(script, "publish_copy", None), max_items=32, max_chars=2_000
        ),
    }

    fact_sources: list[str] = []
    for item in _script_source_items(script):
        if item not in fact_sources:
            fact_sources.append(item)
        if len(fact_sources) >= 64:
            break

    materials: list[dict[str, Any]] = []
    candidate_pool_seen = False
    unknown_locked_rights: list[str] = []
    uncleared_structured_rights: list[str] = []
    material_step = next(
        (
            step
            for step in (getattr(pipeline, "steps", ()) or ())
            if getattr(step, "name", "") == "生成素材任务"
        ),
        None,
    )
    payload = getattr(material_step, "output_summary", {}) if material_step else {}
    tasks = payload.get("material_tasks", []) if isinstance(payload, Mapping) else []

    # Durable writes keep pools in payload["candidate_pool"].  Workspace reads
    # also mirror a pool onto its owning task, so consume both shapes and then
    # de-duplicate them by shot/candidate ID.
    pool_entries: list[tuple[str, Mapping[str, Any]]] = []
    raw_pools = payload.get("candidate_pool") if isinstance(payload, Mapping) else None
    if isinstance(raw_pools, Mapping):
        if isinstance(raw_pools.get("candidates"), list):
            pool_entries.append(("", raw_pools))
        else:
            for pool_shot_id, pool in list(raw_pools.items())[:128]:
                if isinstance(pool, Mapping):
                    pool_entries.append((_bounded_text(pool_shot_id, 160), pool))
    if isinstance(tasks, list):
        for task in tasks[:128]:
            if not isinstance(task, Mapping):
                continue
            pool = task.get("candidate_pool")
            if isinstance(pool, Mapping) and isinstance(pool.get("candidates"), list):
                pool_entries.append((_bounded_text(task.get("shot_id"), 160), pool))
            else:
                # Legacy task snapshots can still carry a direct source URL.
                for key in ("official_source_url", "source_url"):
                    value = _bounded_text(task.get(key), 1_000)
                    if value and value not in fact_sources:
                        fact_sources.append(value)

    seen_pool_keys: set[tuple[str, str]] = set()
    for fallback_shot_id, pool in pool_entries:
        shot_id = _bounded_text(pool.get("shot_id"), 160) or fallback_shot_id
        candidates = pool.get("candidates")
        if not isinstance(candidates, list):
            continue
        candidate_pool_seen = True
        locked_id = _bounded_text(pool.get("locked_candidate_id"), 160)
        if not locked_id:
            locked_id = next(
                (
                    _bounded_text(candidate.get("candidate_id"), 160)
                    for candidate in candidates
                    if isinstance(candidate, Mapping)
                    and candidate.get("status") == "locked"
                    and _bounded_text(candidate.get("candidate_id"), 160)
                ),
                "",
            )
        for candidate in candidates[:16]:
            if not isinstance(candidate, Mapping):
                continue
            candidate_id = _bounded_text(candidate.get("candidate_id"), 160)
            if not candidate_id:
                continue
            if locked_id and candidate_id != locked_id:
                continue
            dedupe_key = (shot_id, candidate_id)
            if dedupe_key in seen_pool_keys:
                continue
            seen_pool_keys.add(dedupe_key)
            rights = _bounded_text(candidate.get("rights"), 1_000)
            if locked_id and _rights_are_unknown(rights):
                unknown_locked_rights.append(shot_id or candidate_id)
            source = _bounded_text(candidate.get("source"), 160)
            source_url = _bounded_text(candidate.get("url"), 1_000)
            raw_rights_evidence = candidate.get("rights_evidence")
            rights_evidence: dict[str, Any] | None = None
            if raw_rights_evidence is not None:
                try:
                    rights_evidence = normalise_stock_rights_evidence(
                        raw_rights_evidence
                    )
                except StockRightsEvidenceError as exc:
                    raise ReleasePackageBlockedError(
                        "rights_evidence_invalid",
                        f"锁定素材的来源许可留证无效：{shot_id or candidate_id}",
                    ) from exc
                if (
                    source.casefold()
                    != str(rights_evidence["provider"]).casefold()
                    or source_url != rights_evidence["asset_page_url"]
                    or rights
                    != rights_evidence["license_snapshot"]["license_name"]
                ):
                    raise ReleasePackageBlockedError(
                        "rights_evidence_invalid",
                        f"锁定素材与来源许可留证不一致：{shot_id or candidate_id}",
                    )
                if (
                    locked_id
                    and rights_evidence["rights_review"]["status"]
                    != "cleared_for_intended_use"
                ):
                    uncleared_structured_rights.append(shot_id or candidate_id)
            material = {
                "shot_id": shot_id,
                "candidate_id": candidate_id,
                "source": source,
                "url": source_url,
                "rights": rights,
                "evidence_kind": _bounded_text(candidate.get("evidence_kind"), 160),
                "status": _bounded_text(candidate.get("status"), 40),
                "reason": _bounded_text(candidate.get("reason"), 1_000),
            }
            if rights_evidence is not None:
                material["rights_evidence"] = rights_evidence
            materials.append(material)

    if unknown_locked_rights:
        raise ReleasePackageBlockedError(
            "rights_unknown",
            "锁定素材缺少授权证据：" + ", ".join(unknown_locked_rights[:8]),
        )
    if uncleared_structured_rights:
        raise ReleasePackageBlockedError(
            "rights_not_cleared",
            "锁定素材尚未完成本次用途清权："
            + ", ".join(uncleared_structured_rights[:8]),
        )

    rights_gate = {}
    gates = quality_report.get("gates")
    if isinstance(gates, Mapping) and isinstance(gates.get("facts_rights"), Mapping):
        rights_gate = dict(gates["facts_rights"])
    rights_status = (
        "manual_attested"
        if rights_gate.get("status") == "passed"
        else "not_verified"
    )
    if not candidate_pool_seen:
        rights_status = "legacy_not_available"
    return {
        "publication_metadata": publication_metadata,
        "cover": {
            "status": "copy_only" if publication_metadata["cover_copy"] else "not_provided",
            "copy": publication_metadata["cover_copy"],
            "image_relative_path": "",
        },
        "fact_sources": fact_sources[:64],
        "materials": materials[:128],
        "rights_review": {
            "status": rights_status,
            "evidence": _bounded_list(
                rights_gate.get("evidence"), max_items=32, max_chars=2_000
            ),
        },
        "ai_disclosure": {
            "status": "manual_required",
            "recommendation": "发布前按目标平台规则确认 AI 配音、合成画面和素材披露要求。",
        },
        "platform_checklist": [
            "确认标题、简介、标签和封面后再发布",
            "确认事实来源、素材授权和署名要求",
            "按目标平台规则完成 AI/合成内容披露",
            "发布后回填平台链接与表现数据到复盘模块",
        ],
    }


def _script_source_items(script: Any) -> list[str]:
    values: list[str] = []
    for entry in getattr(script, "storyboard", None) or []:
        if not isinstance(entry, Mapping):
            continue
        for key in ("source_refs", "fact_sources", "sources"):
            raw = entry.get(key)
            if isinstance(raw, str):
                raw = [raw]
            if isinstance(raw, list):
                values.extend(_bounded_text(item, 1_000) for item in raw)
    return [item for item in values if item]


def _bounded_text(value: Any, max_chars: int) -> str:
    text = str(value or "").strip()
    return text[:max_chars]


def _rights_are_unknown(value: str) -> bool:
    normalized = value.strip().casefold()
    if not normalized:
        return True
    if normalized in {
        "unknown",
        "未确认",
        "不明",
        "未知",
        "unknown rights",
        "unverified",
        "unclear",
        "pending",
        "none",
        "n/a",
        "na",
        "free",
        "member",
        "free/member",
    }:
        return True
    return any(token in normalized for token in ("unknown", "unverified", "pending", "unclear"))


def _bounded_list(
    value: Any,
    *,
    max_items: int,
    max_chars: int,
    depth: int = 0,
) -> list[Any]:
    if not isinstance(value, list):
        return []
    result: list[Any] = []
    for item in value[:max_items]:
        if isinstance(item, Mapping):
            result.append(
                _bounded_mapping(
                    item,
                    max_items=32,
                    max_chars=max_chars,
                    depth=depth + 1,
                )
            )
        else:
            result.append(_bounded_text(item, max_chars))
    return result


def _bounded_mapping(
    value: Any,
    *,
    max_items: int,
    max_chars: int,
    depth: int = 0,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    if depth >= 4:
        return {"_truncated": "nested metadata depth limit"}
    result: dict[str, Any] = {}
    for key, item in list(value.items())[:max_items]:
        key_text = _bounded_text(key, 160)
        if not key_text:
            continue
        if isinstance(item, Mapping):
            result[key_text] = _bounded_mapping(
                item,
                max_items=max_items,
                max_chars=max_chars,
                depth=depth + 1,
            )
        elif isinstance(item, list):
            result[key_text] = _bounded_list(
                item,
                max_items=max_items,
                max_chars=max_chars,
                depth=depth + 1,
            )
        else:
            result[key_text] = _bounded_text(item, max_chars)
    return result


def _existing_release_time(
    pipeline: ProductionPipeline,
    relative: str,
    *,
    expected_artifact_sha: str,
    expected_report_sha: str,
    artifact_relative: str,
    source_relative: str,
    release_report_relative: str,
) -> str:
    try:
        content = _guarded_report_snapshot(pipeline, relative)
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ReleasePackageConflictError("existing release manifest is invalid") from exc
    if not isinstance(payload, dict):
        raise ReleasePackageConflictError("existing release manifest is invalid")
    if (
        payload.get("release_format") != RELEASE_FORMAT
        or payload.get("pipeline_id") != pipeline.id
        or payload.get("source_artifact_relative_path") != source_relative
        or payload.get("artifact_relative_path") != artifact_relative
        or payload.get("artifact_sha256") != expected_artifact_sha
        or payload.get("quality_report_relative_path") != release_report_relative
        or payload.get("quality_report_sha256") != expected_report_sha
        or not isinstance(payload.get("released_at"), str)
    ):
        raise ReleasePackageConflictError("existing release manifest does not match package")
    return payload["released_at"]


def _publish_no_replace(temporary: Path, target: Path) -> None:
    """Publish with the Windows no-replace primitive used by the D: contract."""

    if os.name != "nt":
        raise ReleasePackageCommitUnknownError(
            "release package publication requires Windows no-replace semantics"
        )
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.MoveFileExW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    kernel32.MoveFileExW.restype = wintypes.BOOL
    # MOVEFILE_WRITE_THROUGH, deliberately without REPLACE_EXISTING.
    if not kernel32.MoveFileExW(str(temporary), str(target), 0x00000008):
        error = ctypes.get_last_error()
        if error in {80, 183}:
            raise FileExistsError(error, "release destination already exists", str(target))
        raise OSError(error, "MoveFileExW no-replace failed", str(target))
    try:
        temporary.unlink()
    except FileNotFoundError:
        # MoveFileExW consumes the source path on success.
        return
    except OSError as exc:
        raise ReleasePackageCommitUnknownError(
            "release publish completed but temporary cleanup is unknown"
        ) from exc


def _pipeline_guarded_read(
    pipeline: ProductionPipeline,
    relative: Path,
    reader: Any,
) -> Any:
    try:
        return _read_with_guard(
            pipeline,
            relative,
            system_drive=None,
            reader=reader,
        )
    except (_GuardedFileMissing, _GuardedFileUnsafe, _GuardedFileTooLarge) as exc:
        raise ReleasePackageError("release source is unavailable through the guarded project boundary") from exc


def _guarded_artifact_snapshot(
    pipeline: ProductionPipeline,
    relative: Path,
) -> tuple[str, bool]:
    return _pipeline_guarded_read(pipeline, relative, _read_artifact)


def _guarded_report_snapshot(pipeline: ProductionPipeline, relative: Path | str) -> bytes:
    path = relative if isinstance(relative, Path) else Path(*str(relative).split("/"))
    return _pipeline_guarded_read(pipeline, path, _read_report)


def _canonical_json(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
        "utf-8"
    )


def _is_sha256(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    return all(char in "0123456789abcdef" for char in value)


__all__ = [
    "RELEASE_FORMAT",
    "ReleasePackageBlockedError",
    "ReleasePackageCommitUnknownError",
    "ReleasePackageConflictError",
    "ReleasePackageError",
    "ReleasePackageResult",
    "create_release_package",
]
