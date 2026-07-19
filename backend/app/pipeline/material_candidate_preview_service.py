"""Safe browser access and replacement for reviewed material previews.

The material contract stores a local ``preview_path`` because the renderer and
the provenance audit need a real file.  A browser must never receive that
physical path directly, however: it cannot read a Windows path and exposing it
would leak the machine layout.  This module is the narrow adapter between the
review surface and the D: filesystem.

It intentionally does not download from the network or decide rights.  The
caller still has to provide the provenance fields required by
``material_candidate_contract``; this module only stores an operator-selected
preview under the prepared project directory and applies the immutable
candidate transition.
"""

from __future__ import annotations

from dataclasses import dataclass
import copy
import mimetypes
from pathlib import Path
import re
from typing import BinaryIO
from uuid import uuid4

from sqlalchemy.orm import Session

from app.core.config import get_settings, resolve_configured_path
from app.pipeline.material_candidate_contract import (
    CandidateContractError,
    CandidateEntry,
    CandidateLockedError,
    CandidateNotFoundError,
    CandidateSet,
    replace_candidate,
)
from app.pipeline.material_candidate_media import (
    admit_candidate_media,
    revalidate_candidate_media,
)
from app.pipeline.project_identity import (
    ProjectIdentityError,
    begin_prepared_project_child_directory_use,
    prepared_project_output_path,
    write_prepared_project_files,
)


class CandidatePreviewError(CandidateContractError):
    """Raised when a preview cannot be safely served or stored."""


class CandidatePreviewNotFoundError(FileNotFoundError):
    """Raised when the requested candidate or preview is not available."""


@dataclass(frozen=True, slots=True)
class CandidatePreview:
    """A validated file that can be returned by a route."""

    path: Path
    media_type: str
    filename: str


_ALLOWED_EXTENSIONS = frozenset(
    {
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
        ".mp4",
        ".m4v",
        ".mov",
        ".webm",
    }
)
_ALLOWED_UPLOAD_TYPES = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/webp",
        "video/mp4",
        "video/quicktime",
        "video/webm",
        "video/x-m4v",
    }
)
_SAFE_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
_MAX_PREVIEW_BYTES = 100 * 1024 * 1024


def candidate_preview_url(pipeline_id: int, shot_id: str, candidate_id: str) -> str:
    """Return the only URL shape the UI may use for a local preview."""

    from urllib.parse import quote

    if type(pipeline_id) is not int or pipeline_id <= 0:
        raise ValueError("pipeline_id must be a positive integer")
    shot = _public_segment(shot_id, "shot_id")
    candidate = _public_segment(candidate_id, "candidate_id")
    return (
        f"/api/pipeline/{pipeline_id}/material-tasks/{quote(shot, safe='')}/"
        f"candidate-pool/{quote(candidate, safe='')}/preview"
    )


def resolve_candidate_preview(
    session: Session,
    pipeline_id: int,
    shot_id: str,
    candidate_id: str,
) -> CandidatePreview:
    """Resolve one candidate's physical preview after revalidating its pool."""

    from app.pipeline.material_package_service import (
        _candidate_pool_context,
        _candidate_pool_state,
    )

    pipeline, _step, payload = _candidate_pool_context(session, pipeline_id, shot_id)
    state = _candidate_pool_state(payload, shot_id)
    if state is None:
        raise CandidatePreviewNotFoundError(
            f"candidate pool not found for shot {shot_id!r}"
        )
    try:
        candidate = state.get(candidate_id)
    except CandidateNotFoundError as exc:
        raise CandidatePreviewNotFoundError(str(exc)) from exc

    path = _validated_preview_path(
        candidate.preview_path,
        allowed_roots=_candidate_preview_roots(pipeline),
    )
    media_report = revalidate_candidate_media(candidate)
    return CandidatePreview(
        path=path,
        media_type=media_report.mime_type,
        filename=path.name,
    )


def replace_candidate_with_upload(
    session: Session,
    pipeline_id: int,
    shot_id: str,
    candidate_id: str,
    *,
    upload: BinaryIO,
    filename: str,
    content_type: str | None,
    replacement_candidate_id: str | None,
    source: str,
    url: str = "",
    rights: str,
    evidence_kind: str,
    reason: str,
) -> dict[str, object]:
    """Store an operator-selected preview and replace one unlocked row.

    The upload is bounded, written under the prepared D: project directory,
    and removed again if candidate validation or the database write fails.  A
    locked row is checked before any filesystem mutation.
    """

    from app.pipeline.material_package_service import (
        CandidatePoolCommitUnknownError,
        _candidate_pool_context,
        _candidate_pool_state,
        _persist_candidate_pool,
    )

    pipeline, step, payload = _candidate_pool_context(session, pipeline_id, shot_id)
    state = _candidate_pool_state(payload, shot_id)
    if state is None:
        raise CandidatePreviewNotFoundError(
            f"candidate pool not found for shot {shot_id!r}"
        )
    try:
        current = state.get(candidate_id)
    except CandidateNotFoundError as exc:
        raise CandidatePreviewNotFoundError(str(exc)) from exc
    if current.status == "locked":
        raise CandidateLockedError(
            f"candidate {candidate_id!r} is locked and cannot be replaced"
        )

    safe_name, media_type = _validate_upload_metadata(filename, content_type)
    data = _read_bounded(upload)
    project_dir = str(getattr(pipeline, "project_dir", "") or "").strip()
    if not project_dir:
        raise CandidatePreviewError("project directory is not prepared")
    project_root = Path(project_dir).parent

    directory_use = begin_prepared_project_child_directory_use(
        pipeline,
        project_root=project_root,
        relative_path=Path("素材信息") / "候选预览",
        create=True,
    )
    try:
        new_id = _public_segment(
            (replacement_candidate_id or candidate_id).strip(),
            "replacement_candidate_id",
        )
    except ValueError as exc:
        raise CandidatePreviewError(str(exc)) from exc
    stored_name = _stored_filename(new_id, safe_name)
    final_path = prepared_project_output_path(directory_use, stored_name)
    replacement = CandidateEntry(
        candidate_id=new_id,
        source=source,
        preview_path=str(final_path),
        url=url,
        rights=rights,
        evidence_kind=evidence_kind,
        status="candidate",
        reason=reason,
    )

    try:
        # ``write_prepared_project_files`` uses an exclusive temporary file and
        # verifies the prepared directory before/after the rename.
        write_prepared_project_files(directory_use, {stored_name: data})
        # Verify the durable file before committing the candidate reference.
        # If verification happened after the DB commit, a late filesystem
        # failure could leave a persisted candidate pointing at a file that the
        # exception handler then deletes.
        directory_use.verify()
        replacement = admit_candidate_media(replacement)
        next_state = replace_candidate(state, candidate_id, replacement)
        result = _persist_candidate_pool(
            session,
            step,
            payload,
            next_state,
            pipeline_id=pipeline_id,
        )
        return result
    except CandidatePoolCommitUnknownError:
        # The DB acknowledgement may be ambiguous.  Preserve the uploaded
        # file so a reconciliation pass can determine whether it is already
        # referenced; deleting it here could create a dangling DB path.
        raise
    except BaseException:
        _remove_owned_preview(final_path)
        raise


def _validated_preview_path(raw: str, *, allowed_roots: tuple[Path, ...] = ()) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        raise CandidatePreviewError("preview path must be absolute")
    if path.suffix.casefold() not in _ALLOWED_EXTENSIONS:
        raise CandidatePreviewError("preview file type is not supported")
    # A preview is read-only, but the same D-drive/reparse policy applies: a
    # junction or symlink must never turn this endpoint into arbitrary-file
    # serving.
    try:
        from app.pipeline.project_identity import _reject_reparse_components

        _reject_reparse_components(path, label="candidate preview")
        physical = path.resolve(strict=True)
        if physical.drive.casefold() == "c:":
            raise CandidatePreviewError("candidate preview must be on D: or another non-system drive")
        if not physical.is_file() or physical.stat().st_size <= 0:
            raise CandidatePreviewNotFoundError(f"candidate preview is missing: {path}")
        if physical.stat().st_nlink != 1:
            raise CandidatePreviewError("candidate preview must not be a hard link")
        if allowed_roots and not any(
            _is_within(physical, root) for root in allowed_roots
        ):
            raise CandidatePreviewError("candidate preview is outside approved material roots")
    except CandidatePreviewError:
        raise
    except FileNotFoundError as exc:
        raise CandidatePreviewNotFoundError(f"candidate preview is missing: {path}") from exc
    except (OSError, ProjectIdentityError) as exc:
        raise CandidatePreviewError("candidate preview cannot be read safely") from exc
    return physical


def _candidate_preview_roots(pipeline: object) -> tuple[Path, ...]:
    roots: list[Path] = []
    project_dir = str(getattr(pipeline, "project_dir", "") or "").strip()
    if project_dir:
        roots.append(Path(project_dir))
    settings = get_settings()
    for value in (
        settings.material_root_dir,
        settings.auto_material_root_dir,
        settings.export_dir,
    ):
        try:
            roots.append(resolve_configured_path(value))
        except (OSError, ValueError, TypeError):
            # A malformed configured root must not widen the serving boundary.
            continue
    result: list[Path] = []
    for root in roots:
        try:
            physical = root.resolve(strict=False)
        except OSError:
            continue
        if physical.drive.casefold() != "c:":
            result.append(physical)
    return tuple(result)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _validate_upload_metadata(filename: str, content_type: str | None) -> tuple[str, str]:
    raw = Path(str(filename or "")).name
    if not _SAFE_FILENAME.fullmatch(raw):
        raise CandidatePreviewError("preview filename is invalid")
    suffix = Path(raw).suffix.casefold()
    if suffix not in _ALLOWED_EXTENSIONS:
        raise CandidatePreviewError("preview file type is not supported")
    media_type = (content_type or mimetypes.guess_type(raw)[0] or "").casefold()
    if media_type not in _ALLOWED_UPLOAD_TYPES:
        raise CandidatePreviewError("preview content type is not supported")
    expected = _media_type_for_suffix(suffix)
    if expected != media_type and not (suffix == ".m4v" and media_type == "video/mp4"):
        raise CandidatePreviewError("preview filename and content type do not match")
    return raw, media_type


def _read_bounded(upload: BinaryIO) -> bytes:
    if not hasattr(upload, "read"):
        raise CandidatePreviewError("preview upload is unreadable")
    try:
        data = upload.read(_MAX_PREVIEW_BYTES + 1)
    except (OSError, ValueError) as exc:
        raise CandidatePreviewError("preview upload is unreadable") from exc
    if not isinstance(data, bytes) or not data:
        raise CandidatePreviewError("preview upload is empty")
    if len(data) > _MAX_PREVIEW_BYTES:
        raise CandidatePreviewError("preview upload is too large")
    return data


def _stored_filename(candidate_id: str, original: str) -> str:
    safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", candidate_id).strip(".-")[:48]
    if not safe_id:
        raise CandidatePreviewError("replacement candidate_id is invalid")
    return f"{safe_id}-{uuid4().hex[:12]}{Path(original).suffix.casefold()}"


def _media_type_for(path: Path) -> str:
    return _media_type_for_suffix(path.suffix.casefold())


def candidate_preview_media_type(preview_path: str) -> str:
    """Return a browser media type from a candidate's controlled file suffix.

    This is metadata only; serving still goes through
    :func:`resolve_candidate_preview`, which performs the full root and file
    safety checks.  The public preview URL intentionally has no extension, so
    the UI needs this explicit hint to choose ``<img>`` versus ``<video>``.
    """

    try:
        return _media_type_for(Path(preview_path))
    except (KeyError, TypeError, ValueError):
        return ""


def public_candidate_pool(
    pool: dict[str, object],
    *,
    pipeline_id: int,
    shot_id: str,
) -> dict[str, object]:
    """Return a browser-safe candidate pool DTO.

    ``preview_path`` is an internal renderer/provenance field and must never be
    sent to a browser or leak the operator's D: directory layout.  The route
    URL and explicit media type are the only preview fields exposed publicly.
    """

    result = copy.deepcopy(pool)
    candidates = result.get("candidates")
    if not isinstance(candidates, list):
        return result
    public_candidates: list[dict[str, object]] = []
    allowed_fields = {
        "candidate_id",
        "source",
        "preview_url",
        "preview_media_type",
        "url",
        "rights",
        "evidence_kind",
        "status",
        "reason",
        "media_validation",
        "rights_evidence",
    }
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        candidate_id = candidate.get("candidate_id")
        raw_path = str(candidate.get("preview_path") or "")
        if isinstance(candidate_id, str) and candidate_id.strip():
            candidate["preview_url"] = candidate_preview_url(
                pipeline_id,
                shot_id,
                candidate_id,
            )
            candidate["preview_media_type"] = candidate_preview_media_type(raw_path)
        public_candidates.append(
            {
                key: value
                for key, value in candidate.items()
                if key in allowed_fields
            }
        )
    result["candidates"] = public_candidates
    return result


def _media_type_for_suffix(suffix: str) -> str:
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".mp4": "video/mp4",
        ".m4v": "video/mp4",
        ".mov": "video/quicktime",
        ".webm": "video/webm",
    }[suffix]


def _public_segment(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or "/" in value or "\\" in value:
        raise ValueError(f"{label} is invalid")
    return value.strip()


def _remove_owned_preview(path: Path) -> None:
    try:
        if path.exists() and path.is_file() and not path.is_symlink():
            path.unlink()
    except OSError:
        # The database transition is already considered failed.  Do not mask
        # the original contract/transaction error with cleanup noise.
        pass


__all__ = [
    "CandidatePreview",
    "CandidatePreviewError",
    "CandidatePreviewNotFoundError",
    "candidate_preview_media_type",
    "public_candidate_pool",
    "candidate_preview_url",
    "replace_candidate_with_upload",
    "resolve_candidate_preview",
]
