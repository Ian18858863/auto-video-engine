"""Shared media admission and revalidation for candidate workflow edges."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from app.materials.candidate_media_validation import (
    CandidateMediaReport,
    CandidateMediaValidationError,
    validate_candidate_media,
)
from app.pipeline.material_candidate_contract import (
    CandidateContractError,
    CandidateEntry,
    normalize_candidate,
)


_DECLARATIONS = {
    ".jpg": ("image", "image/jpeg"),
    ".jpeg": ("image", "image/jpeg"),
    ".png": ("image", "image/png"),
    ".webp": ("image", "image/webp"),
    ".mp4": ("video", "video/mp4"),
    ".m4v": ("video", "video/mp4"),
    ".mov": ("video", "video/quicktime"),
    ".webm": ("video", "video/webm"),
}


def admit_candidate_rows(
    candidates: Any,
    *,
    trust_rights_evidence: bool = False,
) -> tuple[CandidateEntry, ...]:
    """Normalize rows and replace claims that only the server may create.

    Browser/API callers are untrusted by default: both byte-validation reports
    and structured stock-rights evidence are discarded.  A worker-owned import
    path may opt in to preserving already validated rights evidence.
    """

    if not isinstance(candidates, Sequence) or isinstance(
        candidates, (str, bytes, bytearray)
    ):
        raise CandidateContractError("candidates must be a list")
    admitted: list[CandidateEntry] = []
    for raw in candidates:
        if isinstance(raw, CandidateEntry):
            candidate = replace(
                raw,
                media_validation=None,
                rights_evidence=(
                    raw.rights_evidence if trust_rights_evidence else None
                ),
            )
        elif isinstance(raw, Mapping):
            sanitized = dict(raw)
            sanitized.pop("media_validation", None)
            if not trust_rights_evidence:
                sanitized.pop("rights_evidence", None)
            candidate = normalize_candidate(sanitized)
        else:
            raise CandidateContractError("candidate must be an object")
        admitted.append(admit_candidate_media(candidate))
    return tuple(admitted)


def admit_candidate_media(candidate: CandidateEntry) -> CandidateEntry:
    """Validate actual bytes and attach the server-computed safe report."""

    report = _validate(candidate)
    return replace(candidate, media_validation=report.to_dict())


def revalidate_candidate_media(candidate: CandidateEntry) -> CandidateMediaReport:
    """Revalidate persisted bytes and reject changes since admission."""

    report = _validate(candidate)
    recorded = candidate.media_validation
    if recorded is not None and (
        recorded.get("sha256") != report.sha256
        or recorded.get("size_bytes") != report.size_bytes
        or recorded.get("media_kind") != report.media_kind
        or recorded.get("mime_type") != report.mime_type
        or recorded.get("width") != report.width
        or recorded.get("height") != report.height
    ):
        raise CandidateContractError(
            f"候选 {candidate.candidate_id!r} 的媒体内容已在入池后变化，请重新生成候选。"
        )
    return report


def _validate(candidate: CandidateEntry) -> CandidateMediaReport:
    suffix = Path(candidate.preview_path).suffix.casefold()
    declaration = _DECLARATIONS.get(suffix)
    if declaration is None:
        raise CandidateContractError(
            f"候选 {candidate.candidate_id!r} 的媒体文件类型不受支持。"
        )
    kind, mime_type = declaration
    try:
        return validate_candidate_media(
            candidate.preview_path,
            declared_kind=kind,
            declared_mime=mime_type,
        )
    except CandidateMediaValidationError as exc:
        raise CandidateContractError(
            f"候选 {candidate.candidate_id!r} 媒体验证失败 [{exc.code}]：{exc}"
        ) from exc


__all__ = [
    "admit_candidate_media",
    "admit_candidate_rows",
    "revalidate_candidate_media",
]
