"""Contained worker for the six static material operations.

The module is intentionally framework-free.  It can be imported by the parent
process for a pure, testable dispatch function, or launched as the isolated
child used by :mod:`material_operation_runner`.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import ntpath
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Callable, Iterable, Mapping, TYPE_CHECKING
from uuid import uuid4

if TYPE_CHECKING:  # pragma: no cover - type checkers only
    from app.core.windows_storage_policy import ApprovedPhysicalDirectory
    from app.pipeline.material_operation_protocol import (
        MaterialOperationRequest,
        MaterialOperationSecrets,
        MaterialWorkerResult,
    )


Adapter = Callable[..., Any]


@dataclass(frozen=True, slots=True)
class MaterialWorkerArtifact:
    """A provider artifact before it is converted to the strict protocol."""

    path: Path
    media_kind: str = ""
    source_type: str = ""
    source_url: str = ""
    evidence_kind: str = "atmosphere"
    is_locally_generated: bool = False
    rights_evidence: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class MaterialWorkerAdapterResult:
    """Provider-neutral result used by injected adapters and built-ins."""

    success: bool
    artifacts: tuple[MaterialWorkerArtifact, ...] = ()
    selected_artifact: Path | str | None = None
    reason_code: str = ""
    reason: str = ""


class _ContainedFingerprintRegistry:
    """Disable canonical fingerprint writes inside an isolated attempt.

    Deduplication/registry publication belongs to the parent process.  The
    provider clients otherwise write a registry JSON beside their downloads,
    which would make the worker mutate staging metadata before the parent has
    verified the artifact tree.
    """

    def find_duplicate(self, file_path: Path):
        del file_path
        return None

    def record_file(self, file_path: Path, *, source_type: str, seen_at=None):
        del file_path, source_type, seen_at
        return None


def _default_official_capture(*, request, secrets, paths) -> MaterialWorkerAdapterResult:
    del secrets
    from app.materials.official_capture import (
        OfficialPageCaptureService,
        build_official_capture_plan,
    )
    from app.materials.provenance import classify_capture_provenance

    payload = request.payload
    browser_cache = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")
    try:
        approved_cache = Path(browser_cache).resolve(strict=True)
    except (OSError, ValueError):
        approved_cache = None
    if (
        approved_cache is None
        or approved_cache.drive.casefold() != "d:"
        or not approved_cache.is_dir()
    ):
        return MaterialWorkerAdapterResult(
            success=False,
            reason_code="browser_cache_unapproved",
            reason="Playwright browser cache must be an existing D directory",
        )
    plan = build_official_capture_plan(
        url=payload.source_url,
        source_name=payload.source_name,
        page_title=payload.page_title,
        paths=paths,
    )
    capture = OfficialPageCaptureService(
        paths=paths,
        use_playwright=True,
        fallback_to_placeholder=False,
        profile_dir=paths.root / "browser-profile",
        downloads_dir=paths.root / "browser-downloads",
    ).capture(plan)
    if not capture.success:
        return MaterialWorkerAdapterResult(
            success=False,
            reason_code="official_capture_failed",
            reason=capture.reason or "official capture failed",
        )
    provenance = classify_capture_provenance(
        payload.source_url,
        used_placeholder=bool(capture.used_placeholder),
    )
    paths_found = [Path(value) for value in capture.files if value]
    if not paths_found and capture.scroll_recording_path:
        paths_found.append(Path(capture.scroll_recording_path))
    artifacts = tuple(
        MaterialWorkerArtifact(
            path=value,
            media_kind="image" if value.suffix.lower() in _IMAGE_SUFFIXES else "video",
            source_type="本地生成" if provenance.is_locally_generated else "官方页面",
            source_url=provenance.source_url,
            evidence_kind=provenance.evidence_kind,
            is_locally_generated=provenance.is_locally_generated,
        )
        for value in paths_found
    )
    if not artifacts:
        return MaterialWorkerAdapterResult(
            success=False,
            reason_code="official_capture_empty",
            reason="official capture produced no artifact",
        )
    return MaterialWorkerAdapterResult(
        success=True,
        artifacts=artifacts,
        selected_artifact=artifacts[0].path,
        reason=capture.reason,
    )


def _default_pexels(*, request, secrets, paths) -> MaterialWorkerAdapterResult:
    from app.materials.pexels import PexelsDownloadService

    payload = request.payload
    service = PexelsDownloadService(
        paths=paths,
        api_key=secrets.payload.api_key,
        fingerprint_registry=_ContainedFingerprintRegistry(),
    )
    result = service.download_one_video_and_one_photo(payload.query, per_page=payload.per_page)
    items = [result.video, result.photo]
    artifacts = tuple(
        MaterialWorkerArtifact(
            path=Path(item.save_path),
            media_kind="image" if item.asset_type == "photo" else "video",
            source_type="Pexels",
            source_url=item.source_url,
            evidence_kind="atmosphere",
            is_locally_generated=False,
            rights_evidence=getattr(item, "rights_evidence", None),
        )
        for item in items
        if item.status == "已下载" and item.save_path
    )
    if not artifacts:
        return MaterialWorkerAdapterResult(
            success=False,
            reason_code="pexels_failed",
            reason="；".join(item.reason for item in items if item.reason)
            or "Pexels produced no artifact",
        )
    return MaterialWorkerAdapterResult(
        success=True,
        artifacts=artifacts,
        selected_artifact=artifacts[0].path,
        reason=f"Pexels downloaded {len(artifacts)} artifact(s)",
    )


def _default_pixabay(*, request, secrets, paths) -> MaterialWorkerAdapterResult:
    from app.materials.source_clients import PixabayDownloadService

    payload = request.payload
    service = PixabayDownloadService(
        paths=paths,
        api_key=secrets.payload.api_key,
        fingerprint_registry=_ContainedFingerprintRegistry(),
    )
    item = service.download_best(payload.query, per_page=payload.per_page)
    if item.status != "已下载" or not item.save_path:
        return MaterialWorkerAdapterResult(
            success=False,
            reason_code="pixabay_failed",
            reason=item.reason or "Pixabay produced no artifact",
        )
    artifact = MaterialWorkerArtifact(
        path=Path(item.save_path),
        media_kind="image" if item.asset_type == "photo" else "video",
        source_type="Pixabay",
        source_url=item.source_url,
        evidence_kind="atmosphere",
        is_locally_generated=False,
        rights_evidence=getattr(item, "rights_evidence", None),
    )
    return MaterialWorkerAdapterResult(
        success=True,
        artifacts=(artifact,),
        selected_artifact=artifact.path,
        reason="Pixabay downloaded an artifact",
    )


def _default_coverr(*, request, secrets, paths) -> MaterialWorkerAdapterResult:
    del secrets
    from app.materials.source_clients import CoverrDownloadService

    payload = request.payload
    item = CoverrDownloadService(
        paths=paths,
        fingerprint_registry=_ContainedFingerprintRegistry(),
    ).download_best(
        payload.query,
        per_page=payload.per_page,
    )
    if item.status != "已下载" or not item.save_path:
        return MaterialWorkerAdapterResult(
            success=False,
            reason_code="coverr_failed",
            reason=item.reason or "Coverr produced no artifact",
        )
    artifact = MaterialWorkerArtifact(
        path=Path(item.save_path),
        media_kind="video",
        source_type="Coverr",
        source_url=item.source_url,
        evidence_kind="atmosphere",
        is_locally_generated=False,
    )
    return MaterialWorkerAdapterResult(
        success=True,
        artifacts=(artifact,),
        selected_artifact=artifact.path,
        reason="Coverr downloaded an artifact",
    )


def _default_info_motion(*, request, secrets, paths) -> MaterialWorkerAdapterResult:
    del secrets
    payload = request.payload
    base = _safe_filename(f"{payload.project_name}_{payload.title}_{uuid4().hex}")
    frame_dir = paths.generated_motion_dir / base / "frames"
    output_path = paths.generated_motion_dir / f"{base}.mp4"
    _write_info_motion_frames(frame_dir, payload.project_name, payload.title)
    _frames_to_video(frame_dir / "motion_%03d.png", output_path)
    artifact = MaterialWorkerArtifact(
        path=output_path,
        media_kind="video",
        source_type="信息动效",
        source_url="",
        evidence_kind="explanation",
        is_locally_generated=True,
    )
    return MaterialWorkerAdapterResult(
        success=True,
        artifacts=(artifact,),
        selected_artifact=artifact.path,
        reason="info motion generated",
    )


@dataclass(slots=True)
class MaterialWorkerServices:
    """Explicit adapter seam.  Each callable receives keyword-only inputs."""

    official_capture: Adapter = _default_official_capture
    pexels: Adapter = _default_pexels
    pixabay: Adapter = _default_pixabay
    coverr: Adapter = _default_coverr
    info_motion: Adapter = _default_info_motion


DEFAULT_SERVICES = MaterialWorkerServices()


def _execute_stock_candidate_adapters(
    *,
    request: Any,
    secrets: Any,
    paths: Any,
    services: MaterialWorkerServices,
    after_provider: Callable[[], None],
) -> MaterialWorkerAdapterResult:
    from app.pipeline.material_operation_protocol import (
        MaterialOperationRequest,
        MaterialOperationSecrets,
        PexelsSecrets,
        PixabaySecrets,
    )

    provider_specs = (
        (
            "pexels",
            services.pexels,
            PexelsSecrets(api_key=secrets.payload.pexels_api_key),
        ),
        (
            "pixabay",
            services.pixabay,
            PixabaySecrets(api_key=secrets.payload.pixabay_api_key),
        ),
    )
    artifacts: list[MaterialWorkerArtifact] = []
    reasons: list[str] = []
    failures: list[str] = []
    for operation, adapter, provider_secret in provider_specs:
        provider_request = MaterialOperationRequest(
            protocol_version=1,
            execution_id=request.execution_id,
            attempt_id=request.attempt_id,
            operation=operation,
            payload=request.payload,
        )
        provider_secrets = MaterialOperationSecrets(
            version=1,
            execution_id=request.execution_id,
            attempt_id=request.attempt_id,
            operation=operation,
            payload=provider_secret,
        )
        try:
            provider_result = _normalize_adapter_result(
                adapter(
                    request=provider_request,
                    secrets=provider_secrets,
                    paths=paths,
                )
            )
        except _WorkerContainmentError:
            raise
        except Exception as exc:
            failures.append(f"{operation} adapter failed: {exc}")
        else:
            if provider_result.success:
                artifacts.extend(provider_result.artifacts)
                if provider_result.reason:
                    reasons.append(provider_result.reason)
            else:
                failures.append(
                    f"{operation} failed: "
                    f"{provider_result.reason or provider_result.reason_code or 'no artifact'}"
                )
        finally:
            after_provider()

    if failures:
        return MaterialWorkerAdapterResult(
            success=False,
            reason_code="stock_sources_failed",
            reason="; ".join(failures),
        )
    return MaterialWorkerAdapterResult(
        success=True,
        artifacts=tuple(artifacts),
        selected_artifact=None,
        reason="; ".join(reasons) or "stock candidate adapters completed",
    )


def execute_material_operation(
    request: "MaterialOperationRequest",
    secrets: "MaterialOperationSecrets",
    attempt_dir: "ApprovedPhysicalDirectory",
    *,
    services: MaterialWorkerServices = DEFAULT_SERVICES,
) -> "MaterialWorkerResult":
    """Execute exactly one operation and return a strict typed result.

    The function never writes outside ``attempt_dir/payload``.  It deliberately
    does not publish ``ready.json`` or ``result.json``; the CLI owns that
    protocol boundary so the pure function remains easy to test.
    """

    from app.core.windows_storage_policy import (
        ApprovedPhysicalDirectory,
        approve_physical_d_write_root,
    )
    from app.pipeline.material_operation_protocol import (
        MaterialOperationRequest,
        MaterialOperationSecrets,
        MaterialProtocolError,
        MaterialWorkerResult,
        StagedArtifact,
    )

    if type(request) is not MaterialOperationRequest:
        raise MaterialProtocolError("request must be MaterialOperationRequest")
    if type(secrets) is not MaterialOperationSecrets:
        raise MaterialProtocolError("secrets must be MaterialOperationSecrets")
    request.__post_init__()
    secrets.__post_init__()
    _validate_secret_match(request, secrets)
    if not isinstance(attempt_dir, ApprovedPhysicalDirectory):
        raise TypeError("attempt_dir must be an ApprovedPhysicalDirectory")
    if not isinstance(services, MaterialWorkerServices):
        raise TypeError("services must be MaterialWorkerServices")
    match request.operation:
        case "official_capture":
            adapter = services.official_capture
        case "pexels":
            adapter = services.pexels
        case "pixabay":
            adapter = services.pixabay
        case "coverr":
            adapter = services.coverr
        case "info_motion":
            adapter = services.info_motion
        case "stock_candidates":
            adapter = None
        case _:
            raise MaterialProtocolError("operation is not allowed")
    if request.operation != "stock_candidates" and not callable(adapter):
        raise MaterialProtocolError("operation adapter is not configured")
    if request.operation == "stock_candidates" and (
        not callable(services.pexels) or not callable(services.pixabay)
    ):
        raise MaterialProtocolError("stock candidate adapters are not configured")
    attempt_dir.verify()
    attempt_root = _require_d_directory(attempt_dir.path)
    payload_pin = approve_physical_d_write_root(attempt_root / "payload")
    child_pins: tuple[Any, ...] = ()
    try:
        _assert_payload_is_direct_child(attempt_dir, payload_pin)
        paths, child_pins = _prepare_contained_material_paths(payload_pin)
        path_snapshot = _snapshot_contained_paths((payload_pin, *child_pins))
    finally:
        for child_pin in reversed(child_pins):
            child_pin.close()
        payload_pin.close()
    return _execute_material_operation_in_payload(
        request,
        secrets,
        paths=paths,
        payload_root=attempt_root / "payload",
        services=services,
        adapter=adapter,
        result_type=MaterialWorkerResult,
        staged_type=StagedArtifact,
        attempt_dir=attempt_dir,
        path_snapshot=path_snapshot,
    )


def _execute_material_operation_in_payload(
    request: Any,
    secrets: Any,
    *,
    paths: Any,
    payload_root: Path,
    services: MaterialWorkerServices,
    adapter: Adapter | None,
    result_type: type,
    staged_type: type,
    attempt_dir: Any,
    path_snapshot: tuple[tuple[Path, str, Any], ...],
) -> Any:
    try:
        if request.operation == "stock_candidates":
            raw = _execute_stock_candidate_adapters(
                request=request,
                secrets=secrets,
                paths=paths,
                services=services,
                after_provider=lambda: _verify_contained_path_snapshot(
                    attempt_dir,
                    path_snapshot,
                ),
            )
        else:
            if adapter is None:
                raise ValueError("operation adapter is not configured")
            raw = adapter(request=request, secrets=secrets, paths=paths)
    except _WorkerContainmentError:
        raise
    except Exception as exc:
        _verify_contained_path_snapshot(attempt_dir, path_snapshot)
        _scrub_payload_secrets(payload_root, secrets)
        return _failed_result(
            request,
            reason_code="adapter_failed",
            reason=_redact_text(str(exc), secrets),
            result_type=result_type,
        )

    _verify_contained_path_snapshot(attempt_dir, path_snapshot)
    normalized = _normalize_adapter_result(raw)
    if not normalized.success:
        _scrub_payload_secrets(payload_root, secrets)
        return _failed_result(
            request,
            reason_code=_redact_text(
                normalized.reason_code or "operation_failed", secrets
            ),
            reason=_redact_text(normalized.reason, secrets),
            result_type=result_type,
        )
    staged_path_map: dict[str, str] = {}
    try:
        artifacts = _stage_artifacts(
            normalized.artifacts,
            payload_root=payload_root,
            operation=request.operation,
            secrets=secrets,
            staged_type=staged_type,
            path_map=staged_path_map,
            deduplicate=request.operation == "stock_candidates",
        )
    except _WorkerContainmentError:
        raise
    except Exception as exc:
        if request.operation != "stock_candidates":
            raise
        _scrub_payload_secrets(payload_root, secrets)
        return _failed_result(
            request,
            reason_code="candidate_batch_invalid",
            reason=_redact_text(str(exc), secrets) or "candidate batch is invalid",
            result_type=result_type,
        )
    if not artifacts:
        return _failed_result(
            request,
            reason_code="no_artifacts",
            reason="operation produced no valid artifact",
            result_type=result_type,
        )
    if request.operation == "stock_candidates":
        batch_error = _stock_candidate_batch_error(artifacts)
        if batch_error:
            _scrub_payload_secrets(payload_root, secrets)
            return _failed_result(
                request,
                reason_code="candidate_batch_invalid",
                reason=batch_error,
                result_type=result_type,
            )
        selected = ""
    else:
        selected = _select_artifact(
            normalized.selected_artifact,
            normalized.artifacts,
            artifacts,
            payload_root=payload_root,
            path_map=staged_path_map,
        )
    if _scrub_payload_secrets(payload_root, secrets):
        return _failed_result(
            request,
            reason_code="secret_payload_rejected",
            reason="payload contained a secret-bearing file or filename",
            result_type=result_type,
        )
    return result_type(
        protocol_version=request.protocol_version,
        execution_id=request.execution_id,
        attempt_id=request.attempt_id,
        operation=request.operation,
        status="succeeded",
        selected_artifact=selected,
        artifacts=tuple(artifacts),
        reason_code="",
        reason=_redact_text(normalized.reason, secrets),
    )


def _assert_payload_is_direct_child(attempt_dir: Any, payload_dir: Any) -> None:
    expected_path = attempt_dir.path / "payload"
    if os.path.normcase(str(payload_dir.path)) != os.path.normcase(
        str(expected_path)
    ):
        raise _WorkerContainmentError(
            "worker payload directory is not the approved attempt child"
        )
    expected_stable = ntpath.join(
        attempt_dir.stable_volume_path.rstrip("\\/"), "payload"
    )
    if ntpath.normcase(payload_dir.stable_volume_path.rstrip("\\/")) != ntpath.normcase(
        expected_stable.rstrip("\\/")
    ):
        raise _WorkerContainmentError(
            "worker payload stable identity is not the approved attempt child"
        )
    attempt_dir.verify()
    payload_dir.verify()


def _prepare_contained_material_paths(payload_dir: Any) -> tuple[Any, tuple[Any, ...]]:
    from app.core.windows_storage_policy import approve_physical_d_write_root
    from app.materials.asset_paths import AutoMaterialPaths

    payload_root = payload_dir.path
    _validate_existing_payload_tree(payload_root)
    try:
        if any(payload_root.iterdir()):
            raise _WorkerContainmentError(
                "worker payload directory must be empty before an operation"
            )
    except OSError as exc:
        raise _WorkerContainmentError(
            "worker payload directory cannot be inspected"
        ) from exc

    paths = AutoMaterialPaths(root=payload_root)
    directories = (
        paths.backup_dir,
        paths.auto_download_dir,
        paths.pexels_root,
        paths.pexels_video_dir,
        paths.pexels_photo_dir,
        paths.pixabay_root,
        paths.pixabay_video_dir,
        paths.pixabay_photo_dir,
        paths.coverr_root,
        paths.coverr_video_dir,
        paths.official_page_dir,
        paths.generated_motion_dir,
        payload_root / "browser-profile",
        payload_root / "browser-downloads",
    )
    known: dict[str, Any] = {
        os.path.normcase(str(payload_root)): payload_dir,
    }
    created: list[Any] = []
    for directory in directories:
        relative = directory.relative_to(payload_root)
        parent = payload_dir
        current = payload_root
        for part in relative.parts:
            current = current / part
            key = os.path.normcase(str(current))
            existing_pin = known.get(key)
            if existing_pin is not None:
                parent = existing_pin
                continue
            try:
                if current.exists():
                    child = approve_physical_d_write_root(current)
                else:
                    child = parent.create_exclusive_child(part)
            except Exception as exc:
                for pin in reversed(created):
                    pin.close()
                raise _WorkerContainmentError(
                    "worker material directory could not be created exclusively"
                ) from exc
            known[key] = child
            created.append(child)
            parent = child
    payload_dir.verify()
    return paths, tuple(created)


def _validate_existing_payload_tree(root: Path) -> None:
    def visit(directory: Path) -> None:
        try:
            entries = tuple(directory.iterdir())
        except OSError as exc:
            raise _WorkerContainmentError(
                "worker payload directory cannot be inspected"
            ) from exc
        for entry in entries:
            try:
                metadata = entry.lstat()
            except OSError as exc:
                raise _WorkerContainmentError(
                    "worker payload entry cannot be inspected"
                ) from exc
            attributes = int(getattr(metadata, "st_file_attributes", 0))
            if entry.is_symlink() or attributes & 0x0400:
                raise _WorkerContainmentError(
                    "worker payload entry cannot contain a reparse point"
                )
            if entry.is_dir():
                visit(entry)
            elif metadata.st_nlink != 1:
                raise ValueError("worker payload entry is not a single-link file")

    visit(root)


def _snapshot_contained_paths(
    directories: tuple[Any, ...],
) -> tuple[tuple[Path, str, Any], ...]:
    return tuple(
        (directory.path, directory.stable_volume_path, directory.identity)
        for directory in directories
    )


def _verify_contained_path_snapshot(
    attempt_dir: Any,
    snapshot: tuple[tuple[Path, str, Any], ...],
) -> None:
    from app.core.windows_storage_policy import approve_physical_d_write_root

    attempt_dir.verify()
    for path, stable_path, identity in snapshot:
        current = approve_physical_d_write_root(path)
        try:
            if (
                os.path.normcase(str(current.path)) != os.path.normcase(str(path))
                or ntpath.normcase(current.stable_volume_path.rstrip("\\/"))
                != ntpath.normcase(stable_path.rstrip("\\/"))
                or current.identity != identity
            ):
                raise _WorkerContainmentError(
                    "worker material directory identity changed"
                )
        finally:
            current.close()


class _WorkerContainmentError(ValueError):
    pass


def _validate_secret_match(request: Any, secrets: Any) -> None:
    if (
        secrets.execution_id != request.execution_id
        or secrets.attempt_id != request.attempt_id
        or secrets.operation != request.operation
        or secrets.version != request.protocol_version
    ):
        raise ValueError("secret envelope does not match request")


def _failed_result(request: Any, *, reason_code: str, reason: str, result_type: type) -> Any:
    return result_type(
        protocol_version=request.protocol_version,
        execution_id=request.execution_id,
        attempt_id=request.attempt_id,
        operation=request.operation,
        status="failed",
        selected_artifact="",
        artifacts=(),
        reason_code=_safe_reason_code(reason_code),
        reason=reason or "operation failed",
    )


def _stock_candidate_batch_error(artifacts: list[Any]) -> str:
    if not 3 <= len(artifacts) <= 5:
        return "stock candidate batch must contain 3 to 5 deduplicated artifacts"
    sources = {item.source_type for item in artifacts}
    if not sources.issubset({"Pexels", "Pixabay"}) or not {
        "Pexels",
        "Pixabay",
    }.issubset(sources):
        return "stock candidate batch must contain Pexels and Pixabay artifacts"
    if len({item.sha256 for item in artifacts}) != len(artifacts):
        return "stock candidate batch contains duplicate fingerprints"
    source_identities = {(item.source_type, item.source_url) for item in artifacts}
    if len(source_identities) != len(artifacts):
        return "stock candidate batch contains duplicate provider sources"
    if any(
        item.is_locally_generated
        or item.evidence_kind == "test_placeholder"
        or not item.source_url
        for item in artifacts
    ):
        return "stock candidate batch requires real provider artifacts"
    for item in artifacts:
        rights = item.rights_evidence
        if not isinstance(rights, Mapping):
            return "stock candidate batch requires structured rights evidence"
        if rights.get("provider") != item.source_type:
            return "stock candidate rights evidence provider must match source type"
        media_type = rights.get("media_type")
        media_matches = (
            media_type == "video"
            if item.media_kind == "video"
            else media_type in {"photo", "illustration", "vector"}
        )
        if not media_matches:
            return "stock candidate rights evidence media type must match media kind"
    return ""


def _normalize_adapter_result(raw: Any) -> MaterialWorkerAdapterResult:
    if isinstance(raw, MaterialWorkerAdapterResult):
        return raw
    if isinstance(raw, Mapping):
        raw_artifacts = raw.get("artifacts", ())
        return MaterialWorkerAdapterResult(
            success=bool(raw.get("success", False)),
            artifacts=tuple(_coerce_artifact(value) for value in raw_artifacts or ()),
            selected_artifact=raw.get("selected_artifact"),
            reason_code=str(raw.get("reason_code") or ""),
            reason=str(raw.get("reason") or ""),
        )
    if isinstance(raw, (Path, str)):
        return MaterialWorkerAdapterResult(
            success=True,
            artifacts=(_coerce_artifact(raw),),
            selected_artifact=raw,
        )
    raise ValueError("adapter returned an unsupported result")


def _coerce_artifact(value: Any) -> MaterialWorkerArtifact:
    if isinstance(value, MaterialWorkerArtifact):
        return value
    if isinstance(value, (Path, str)):
        return MaterialWorkerArtifact(path=Path(value))
    if isinstance(value, Mapping):
        return MaterialWorkerArtifact(
            path=Path(value.get("path") or value.get("file_path") or ""),
            media_kind=str(value.get("media_kind") or ""),
            source_type=str(value.get("source_type") or ""),
            source_url=str(value.get("source_url") or ""),
            evidence_kind=str(value.get("evidence_kind") or "atmosphere"),
            is_locally_generated=bool(value.get("is_locally_generated", False)),
            rights_evidence=(
                value.get("rights_evidence")
                if isinstance(value.get("rights_evidence"), Mapping)
                else None
            ),
        )
    raise ValueError("adapter artifact is unsupported")


def _stage_artifacts(
    artifacts: Iterable[MaterialWorkerArtifact],
    *,
    payload_root: Path,
    operation: str,
    secrets: Any,
    staged_type: type,
    path_map: dict[str, str] | None = None,
    deduplicate: bool = False,
) -> list[Any]:
    staged: list[Any] = []
    seen: set[str] = set()
    relative_paths: dict[str, str] = {}
    fingerprint_paths: dict[str, tuple[Path, str]] = {}
    source_paths: dict[tuple[str, str], tuple[Path, str]] = {}
    for artifact in artifacts:
        path = artifact.path if isinstance(artifact.path, Path) else Path(artifact.path)
        if not path.is_absolute():
            path = payload_root / path
        resolved = _resolve_contained_regular_file(path, payload_root=payload_root)
        original_path_key = _path_key(path)
        sanitized = _sanitize_artifact_filename(resolved, secrets)
        resolved = _resolve_contained_regular_file(
            sanitized,
            payload_root=payload_root,
        )
        relative = resolved.relative_to(payload_root)
        if not relative.parts or any(part in {".", ".."} for part in relative.parts):
            raise _WorkerContainmentError("artifact path contains an invalid relative component")
        relative_name = "payload/" + relative.as_posix()
        if path_map is not None:
            path_map[original_path_key] = relative_name
            path_map[_path_key(resolved)] = relative_name
        key = relative_name.casefold()
        if key in seen:
            if deduplicate:
                if path_map is not None:
                    path_map[original_path_key] = relative_paths[key]
                continue
            raise ValueError("artifact paths must be unique")
        seen.add(key)
        relative_paths[key] = relative_name
        size = resolved.stat().st_size
        if size <= 0:
            raise ValueError("artifact must not be empty")
        media_kind = artifact.media_kind or (
            "image" if resolved.suffix.lower() in _IMAGE_SUFFIXES else "video"
        )
        if media_kind not in {"video", "image"}:
            raise ValueError("artifact media_kind is not allowed")
        source_type = _redact_text(
            artifact.source_type or _default_source_type(operation),
            secrets,
        )
        evidence_kind = artifact.evidence_kind or _default_evidence_kind(operation)
        local = bool(artifact.is_locally_generated)
        source_url = _redact_url(artifact.source_url, secrets)
        if operation == "info_motion":
            evidence_kind = "explanation"
            local = True
            source_url = ""
        digest = _sha256_file(resolved)
        if artifact.rights_evidence is not None and _metadata_contains_secret(
            artifact.rights_evidence,
            secrets,
        ):
            raise ValueError("artifact rights_evidence contains a secret-bearing value")
        staged_artifact = staged_type(
            relative_path=relative_name,
            media_kind=media_kind,
            source_type=source_type,
            source_url=source_url,
            evidence_kind=evidence_kind,
            is_locally_generated=local,
            sha256=digest,
            size_bytes=size,
            rights_evidence=artifact.rights_evidence,
        )
        if deduplicate:
            duplicate = fingerprint_paths.get(digest)
            source_identity = (source_type, source_url)
            if duplicate is None and source_url:
                duplicate = source_paths.get(source_identity)
            if duplicate is not None:
                kept_path, kept_relative = duplicate
                if path_map is not None:
                    path_map[original_path_key] = kept_relative
                    path_map[_path_key(resolved)] = kept_relative
                if resolved != kept_path:
                    resolved.unlink()
                continue
            fingerprint_paths[digest] = (resolved, relative_name)
            if source_url:
                source_paths[source_identity] = (resolved, relative_name)
        staged.append(staged_artifact)
    return staged


def _select_artifact(
    selected: Path | str | None,
    raw_artifacts: tuple[MaterialWorkerArtifact, ...],
    staged: list[Any],
    *,
    payload_root: Path,
    path_map: Mapping[str, str] | None = None,
) -> str:
    if not staged:
        raise ValueError("cannot select from an empty artifact list")
    if selected is None:
        return staged[0].relative_path
    try:
        selected_path = Path(selected)
        if isinstance(selected, str) and selected.startswith("payload/"):
            selected_path = payload_root / selected.removeprefix("payload/")
        elif not selected_path.is_absolute():
            selected_path = payload_root / selected_path
        if path_map is not None:
            mapped = path_map.get(_path_key(selected_path))
            if mapped:
                return mapped
        selected_resolved = selected_path.resolve(strict=True)
    except (OSError, TypeError, ValueError):
        selected_resolved = None
    if selected_resolved is not None:
        for raw, normalized in zip(raw_artifacts, staged):
            try:
                if raw.path.resolve(strict=True) == selected_resolved:
                    return normalized.relative_path
            except OSError:
                continue
    if isinstance(selected, str) and selected.startswith("payload/"):
        if any(item.relative_path == selected for item in staged):
            return selected
    raise ValueError("selected_artifact does not match an artifact")


def _path_key(path: Path) -> str:
    return os.path.normcase(str(path.absolute()))


def _require_d_directory(path: Path) -> Path:
    resolved = path.resolve(strict=True)
    drive = str(resolved).replace("/", "\\")[:2].casefold()
    if drive != "d:" or not resolved.is_dir():
        raise ValueError("attempt directory must be an existing physical D directory")
    return resolved


def _default_source_type(operation: str) -> str:
    return {
        "official_capture": "官方页面",
        "pexels": "Pexels",
        "pixabay": "Pixabay",
        "coverr": "Coverr",
        "info_motion": "信息动效",
    }.get(operation, operation)


def _default_evidence_kind(operation: str) -> str:
    return "explanation" if operation == "info_motion" else "atmosphere"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _secret_values(secrets: Any) -> tuple[str, ...]:
    payload = getattr(secrets, "payload", None)
    values: list[str] = []
    for field_name in ("api_key", "pexels_api_key", "pixabay_api_key"):
        value = getattr(payload, field_name, "")
        if isinstance(value, str) and value and value not in values:
            values.append(value)
    return tuple(values)


def _metadata_contains_secret(value: Any, secrets: Any) -> bool:
    secret_values = _secret_values(secrets)
    if not secret_values:
        return False
    pending = [value]
    seen: set[int] = set()
    nodes = 0
    while pending:
        current = pending.pop()
        nodes += 1
        if nodes > 4096:
            return True
        if isinstance(current, str):
            if any(secret in current for secret in secret_values):
                return True
            continue
        if isinstance(current, bytes):
            if any(secret.encode("utf-8") in current for secret in secret_values):
                return True
            continue
        if isinstance(current, Mapping):
            if id(current) in seen:
                continue
            seen.add(id(current))
            pending.extend(current.keys())
            pending.extend(current.values())
            continue
        if isinstance(current, (list, tuple, set, frozenset)):
            if id(current) in seen:
                continue
            seen.add(id(current))
            pending.extend(current)
    return False


def _redact_url(value: str, secrets: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    text = _redact_query_url(text)
    for secret in _secret_values(secrets):
        text = text.replace(secret, "<redacted>")
    return text


def _redact_text(value: str, secrets: Any) -> str:
    text = str(value or "")
    text = re.sub(r"https?://[^\s]+", lambda match: _redact_query_url(match.group(0)), text)
    for secret in _secret_values(secrets):
        text = text.replace(secret, "<redacted>")
    return text


def _redact_query_url(value: str) -> str:
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    try:
        parts = urlsplit(value)
        if not parts.query:
            return value
        query = urlencode(
            [
                (key, "<redacted>" if item else item)
                for key, item in parse_qsl(parts.query, keep_blank_values=True)
            ]
        )
        return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))
    except ValueError:
        return value


def _scrub_payload_secrets(payload_root: Path, secrets: Any) -> bool:
    secret_values = _secret_values(secrets)
    if not secret_values:
        return False
    needles = tuple(secret.encode("utf-8") for secret in secret_values)
    changed = False

    def scrub_directory(directory: Path) -> None:
        nonlocal changed
        try:
            entries = tuple(directory.iterdir())
        except OSError as exc:
            raise _WorkerContainmentError(
                "payload secret scan could not inspect a directory"
            ) from exc
        for entry in entries:
            try:
                metadata = entry.lstat()
            except OSError as exc:
                raise _WorkerContainmentError(
                    "payload secret scan could not inspect an entry"
                ) from exc
            attributes = int(getattr(metadata, "st_file_attributes", 0))
            if entry.is_symlink() or attributes & 0x0400:
                raise _WorkerContainmentError(
                    "payload secret scan found a reparse point"
                )
            if entry.is_dir():
                if any(secret in entry.name for secret in secret_values):
                    renamed = entry.with_name(f"redacted_{uuid4().hex}")
                    entry.rename(renamed)
                    entry = renamed
                    changed = True
                scrub_directory(entry)
                continue
            contains_secret = any(
                _file_contains_bytes(entry, needle) for needle in needles
            )
            if contains_secret:
                entry.unlink()
                changed = True
            elif any(secret in entry.name for secret in secret_values):
                sanitized_name = entry.name
                for secret in secret_values:
                    sanitized_name = sanitized_name.replace(secret, "redacted")
                if not sanitized_name or sanitized_name == entry.name:
                    sanitized_name = f"redacted_{uuid4().hex}{entry.suffix}"
                renamed = entry.with_name(sanitized_name)
                if renamed.exists():
                    renamed = entry.with_name(f"redacted_{uuid4().hex}{entry.suffix}")
                entry.rename(renamed)
                changed = True

    scrub_directory(payload_root)
    return changed


def _file_contains_bytes(path: Path, needle: bytes) -> bool:
    if not needle:
        return False
    overlap = b""
    try:
        with path.open("rb") as stream:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    return False
                data = overlap + chunk
                if needle in data:
                    return True
                overlap = data[-(len(needle) - 1) :] if len(needle) > 1 else b""
    except OSError as exc:
        raise _WorkerContainmentError(
            "payload secret scan could not read an artifact"
        ) from exc


def _safe_reason_code(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9_]+", "_", str(value or "operation_failed").lower()).strip("_")
    return (cleaned or "operation_failed")[:64]


def _safe_filename(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\r\n\t]+', "_", str(value)).strip(" ._")
    cleaned = re.sub(r"\s+", "_", cleaned)
    return cleaned[:96] or f"material_{uuid4().hex}"


def _sanitize_artifact_filename(path: Path, secrets: Any) -> Path:
    secret_values = _secret_values(secrets)
    if not secret_values or not any(secret in path.name for secret in secret_values):
        return path
    sanitized_name = path.name
    for secret in secret_values:
        sanitized_name = sanitized_name.replace(secret, "redacted")
    if not sanitized_name or sanitized_name == path.name:
        sanitized_name = f"artifact_{uuid4().hex}{path.suffix}"
    target = path.with_name(sanitized_name)
    if target.exists():
        target = path.with_name(f"artifact_{uuid4().hex}{path.suffix}")
    path.replace(target)
    return target


def _resolve_contained_regular_file(path: Path, *, payload_root: Path) -> Path:
    try:
        unresolved = path.absolute()
        unresolved.relative_to(payload_root)
    except (OSError, ValueError) as exc:
        raise _WorkerContainmentError(
            "artifact path must remain inside the approved payload directory"
        ) from exc
    _reject_reparse_components(unresolved, payload_root=payload_root)
    try:
        resolved = unresolved.resolve(strict=True)
        resolved.relative_to(payload_root)
    except (OSError, ValueError) as exc:
        raise _WorkerContainmentError(
            "artifact path must remain inside the approved payload directory"
        ) from exc
    stat_result = resolved.stat()
    if not resolved.is_file() or stat_result.st_nlink != 1:
        raise ValueError("artifact path is not one ordinary single-link file")
    return resolved


def _reject_reparse_components(path: Path, *, payload_root: Path) -> None:
    relative = path.relative_to(payload_root)
    current = payload_root
    for part in relative.parts:
        current = current / part
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise ValueError("artifact path cannot be inspected") from exc
        attributes = int(getattr(metadata, "st_file_attributes", 0))
        if current.is_symlink() or attributes & 0x0400:
            raise _WorkerContainmentError(
                "artifact path cannot contain a reparse point"
            )


_IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif"})


def _write_info_motion_frames(frame_dir: Path, project_name: str, title: str) -> None:
    from PIL import Image, ImageDraw

    frame_dir.mkdir(parents=True, exist_ok=True)
    for index in range(12):
        image = Image.new("RGB", (1080, 1920), (16, 28, 42))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((72, 120, 1008, 480), radius=32, fill=(30, 78, 110))
        draw.text((120, 180), str(project_name)[:40], fill=(240, 248, 255))
        draw.text((120, 310), str(title)[:48], fill=(184, 222, 240))
        draw.text((120, 620 + index * 4), f"信息动效 {index + 1:02d}", fill=(235, 240, 245))
        image.save(frame_dir / f"motion_{index:03d}.png")


def _frames_to_video(pattern: Path, output_path: Path, *, duration_seconds: int = 6, fps: int = 12) -> None:
    import imageio_ffmpeg

    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        imageio_ffmpeg.get_ffmpeg_exe(),
        "-y",
        "-framerate",
        str(max(1, fps)),
        "-i",
        str(pattern),
        "-t",
        str(max(1, duration_seconds)),
        "-vf",
        "scale=1080:1920:force_original_aspect_ratio=increase:flags=lanczos,crop=1080:1920,fps=30,format=yuv420p",
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "20",
        str(output_path),
    ]
    completed = subprocess.run(command, capture_output=True, timeout=60)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or b"").decode("utf-8", errors="replace")
        raise RuntimeError((detail or "ffmpeg failed")[-1200:])


def _parse_cli_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend-root", required=True)
    parser.add_argument("--site-packages", required=True)
    parser.add_argument("--attempt-dir", required=True)
    parser.add_argument("--pipe-name", required=True)
    return parser.parse_args(argv)


def _read_secret_frame(pipe_handle: Any, *, maximum_bytes: int) -> bytes:
    import win32file

    frame = bytearray()
    while len(frame) < 4:
        status, chunk = win32file.ReadFile(pipe_handle, 4 - len(frame))
        if status not in {0, None}:
            raise RuntimeError("secret pipe read failed")
        if not chunk:
            raise RuntimeError("secret pipe closed before length prefix")
        frame.extend(bytes(chunk))
    payload_length = int.from_bytes(frame[:4], "big")
    if not 1 <= payload_length <= maximum_bytes:
        raise RuntimeError("secret frame length is outside protocol bounds")
    while len(frame) < 4 + payload_length:
        status, chunk = win32file.ReadFile(pipe_handle, 4 + payload_length - len(frame))
        if status not in {0, None}:
            raise RuntimeError("secret pipe read failed")
        if not chunk:
            raise RuntimeError("secret pipe closed before payload")
        frame.extend(bytes(chunk))
    if len(frame) != 4 + payload_length:
        raise RuntimeError("secret frame has trailing bytes")
    _reject_trailing_secret_bytes(pipe_handle, win32file=win32file)
    return bytes(frame[4:])


def _reject_trailing_secret_bytes(pipe_handle: Any, *, win32file: Any) -> None:
    """Require the runner to close the outbound pipe immediately after one frame."""

    try:
        status, chunk = win32file.ReadFile(pipe_handle, 1)
    except Exception as exc:
        # ERROR_BROKEN_PIPE / ERROR_PIPE_NOT_CONNECTED are the normal EOF
        # signals after the runner closes its outbound handle.
        if getattr(exc, "winerror", None) in {109, 233}:
            return
        raise RuntimeError("secret pipe trailing-byte check failed") from exc
    if status in {109, 233}:
        return
    if status not in {0, None}:
        raise RuntimeError("secret pipe trailing-byte check failed")
    if chunk:
        raise RuntimeError("secret pipe contains trailing bytes")


def _prepare_cli_runtime(arguments: argparse.Namespace) -> Any:
    backend_root = Path(arguments.backend_root).resolve(strict=True)
    site_packages = Path(arguments.site_packages).resolve(strict=True)
    for path in (
        site_packages / "pythonwin",
        site_packages / "win32" / "lib",
        site_packages / "win32",
        site_packages,
        backend_root,
    ):
        sys.path.insert(0, str(path))
    add_dll = getattr(os, "add_dll_directory", None)
    if add_dll is None:
        return None
    return add_dll(str(site_packages / "pywin32_system32"))


def main(argv: list[str] | None = None) -> int:
    arguments = _parse_cli_arguments(argv)
    dll_directory = _prepare_cli_runtime(arguments)
    try:
        return _main_with_runtime(arguments)
    except Exception:
        # The runner receives the durable result/termination evidence; never
        # print provider exceptions because they can contain credentials.
        return 2
    finally:
        if dll_directory is not None:
            try:
                dll_directory.close()
            except Exception:
                pass


def _main_with_runtime(arguments: argparse.Namespace) -> int:
    import win32con
    import win32file

    from app.core.windows_durable_file import write_durable_bytes
    from app.core.windows_storage_policy import approve_physical_d_write_root
    from app.pipeline.material_operation_control_protocol import (
        MAX_READY_BYTES,
        MaterialWorkerReady,
        encode_material_worker_ready,
    )
    from app.pipeline.material_operation_protocol import (
        MAX_RESULT_BYTES,
        MAX_SECRET_BYTES,
        decode_material_operation_request,
        decode_material_operation_secrets,
        encode_material_worker_result,
    )

    attempt_path = _require_d_directory(Path(arguments.attempt_dir))
    request_bytes = (attempt_path / "request.json").read_bytes()
    request = decode_material_operation_request(request_bytes)
    pipe_handle = win32file.CreateFile(
        arguments.pipe_name,
        win32con.GENERIC_READ,
        0,
        None,
        win32con.OPEN_EXISTING,
        0,
        None,
    )
    try:
        secret_bytes = _read_secret_frame(pipe_handle, maximum_bytes=MAX_SECRET_BYTES)
    finally:
        pipe_handle.Close()
    secrets = decode_material_operation_secrets(secret_bytes, request=request)
    request_sha256 = hashlib.sha256(request_bytes).hexdigest()
    ready = MaterialWorkerReady(
        protocol_version=1,
        state="ready",
        execution_id=request.execution_id,
        attempt_id=request.attempt_id,
        operation=request.operation,
        root_pid=os.getpid(),
        request_sha256=request_sha256,
    )
    approved = approve_physical_d_write_root(attempt_path)
    try:
        write_durable_bytes(
            parent=approved,
            filename="ready.json",
            payload=encode_material_worker_ready(ready),
            replace=False,
            max_bytes=MAX_READY_BYTES,
        )
        try:
            result = execute_material_operation(request, secrets, approved)
        except Exception as exc:
            from app.pipeline.material_operation_protocol import MaterialWorkerResult

            result = MaterialWorkerResult(
                protocol_version=request.protocol_version,
                execution_id=request.execution_id,
                attempt_id=request.attempt_id,
                operation=request.operation,
                status="failed",
                selected_artifact="",
                artifacts=(),
                reason_code="worker_failed",
                reason=_redact_text(str(exc), secrets),
            )
        write_durable_bytes(
            parent=approved,
            filename="result.json",
            payload=encode_material_worker_result(result),
            replace=False,
            max_bytes=MAX_RESULT_BYTES,
        )
    finally:
        approved.close()
    return 0


__all__ = [
    "DEFAULT_SERVICES",
    "MaterialWorkerAdapterResult",
    "MaterialWorkerArtifact",
    "MaterialWorkerServices",
    "execute_material_operation",
    "main",
]


if __name__ == "__main__":  # pragma: no cover - exercised by the Windows runner
    raise SystemExit(main())
