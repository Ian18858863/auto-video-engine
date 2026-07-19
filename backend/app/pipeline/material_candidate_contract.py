"""Small, route-free contract for shot-level material candidates.

This module deliberately does not search, download, or manufacture a
candidate.  A caller must provide a real local preview and the provenance
fields before a row can enter a :class:`CandidateSet`.  The state helpers are
pure: they return a new immutable snapshot and never mutate the input.

The legacy material code has several candidate shapes (``CandidateMaterial``
uses ``material_id``/``local_path``/``license_type``).  ``normalize_candidate``
accepts those names as read-only compatibility aliases, while the canonical
contract uses the names shared by the review surface:

``candidate_id, source, preview_path, url, rights, evidence_kind, status,
reason, media_validation, rights_evidence``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
import re
from typing import Any, Literal
from urllib.parse import urlparse

from app.materials.stock_rights_evidence import (
    StockRightsEvidenceError,
    normalise_stock_rights_evidence,
)

CandidateStatus = Literal["candidate", "locked", "rejected"]

STATUS_CANDIDATE: CandidateStatus = "candidate"
STATUS_LOCKED: CandidateStatus = "locked"
STATUS_REJECTED: CandidateStatus = "rejected"

# These are the evidence labels already used by ``materials.provenance`` plus
# the two explicit non-page sources called out by the project contract.
EVIDENCE_KINDS = frozenset(
    {
        "atmosphere",
        "direct_evidence",
        "explanation",
        "test_placeholder",
        "self_owned",
        "licensed",
        "programmatic",
    }
)

_UNKNOWN_RIGHTS = frozenset(
    {
        "",
        "unknown",
        "unverified",
        "unclear",
        "pending",
        "none",
        "n/a",
        "na",
        "free",
        "member",
        "free/member",
    }
)
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


class CandidateContractError(ValueError):
    """Raised when a candidate or candidate set is not reviewable."""


class CandidateLockedError(CandidateContractError):
    """Raised when an operation would silently overwrite a locked choice."""


class CandidateNotFoundError(CandidateContractError):
    """Raised when a transition references an unknown candidate ID."""


@dataclass(frozen=True, slots=True)
class CandidateEntry:
    """Canonical review row for one real material candidate."""

    candidate_id: str
    source: str
    preview_path: str
    url: str
    rights: str
    evidence_kind: str
    status: CandidateStatus = STATUS_CANDIDATE
    reason: str = ""
    media_validation: dict[str, Any] | None = None
    rights_evidence: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        # Direct construction is supported for callers that already have a
        # typed row.  Full path/existence checks happen in
        # ``validate_candidate_set`` so this dataclass remains cheap and
        # deterministic to use in pure transitions.
        if self.status not in {
            STATUS_CANDIDATE,
            STATUS_LOCKED,
            STATUS_REJECTED,
        }:
            raise CandidateContractError(f"unsupported candidate status: {self.status!r}")
        for field_name in (
            "candidate_id",
            "source",
            "preview_path",
            "rights",
            "evidence_kind",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise CandidateContractError(f"{field_name} is required")
            if _CONTROL_CHARS.search(value):
                raise CandidateContractError(f"{field_name} contains control characters")
            if field_name == "candidate_id" and ("/" in value or "\\" in value):
                raise CandidateContractError("candidate_id must not contain path separators")
        if not isinstance(self.url, str):
            raise CandidateContractError("url must be a string")
        if _CONTROL_CHARS.search(self.url) or _CONTROL_CHARS.search(self.reason):
            raise CandidateContractError("candidate text contains control characters")
        object.__setattr__(
            self,
            "media_validation",
            _normalise_media_validation(self.media_validation),
        )
        canonical_rights = _normalise_rights_evidence(self.rights_evidence)
        if canonical_rights is not None:
            if self.source.casefold() != str(canonical_rights["provider"]).casefold():
                raise CandidateContractError(
                    "candidate source must match rights_evidence provider"
                )
            if self.url != canonical_rights["asset_page_url"]:
                raise CandidateContractError(
                    "candidate url must match rights_evidence asset_page_url"
                )
            if self.rights != canonical_rights["license_snapshot"]["license_name"]:
                raise CandidateContractError(
                    "candidate rights must match rights_evidence license_name"
                )
        object.__setattr__(self, "rights_evidence", canonical_rights)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "candidate_id": self.candidate_id,
            "source": self.source,
            "preview_path": self.preview_path,
            "url": self.url,
            "rights": self.rights,
            "evidence_kind": self.evidence_kind,
            "status": self.status,
            "reason": self.reason,
        }
        if self.media_validation is not None:
            result["media_validation"] = dict(self.media_validation)
        if self.rights_evidence is not None:
            result["rights_evidence"] = normalise_stock_rights_evidence(
                self.rights_evidence
            )
        return result


# Names that make the contract easy to discover from the existing material
# vocabulary.  They are aliases, not second mutable models.
MaterialCandidate = CandidateEntry
CandidateMaterial = CandidateEntry


@dataclass(frozen=True, slots=True)
class CandidateSet:
    """Immutable 3–5 candidate snapshot for one shot."""

    shot_id: str
    candidates: tuple[CandidateEntry, ...]
    revision: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.shot_id, str) or not self.shot_id.strip():
            raise CandidateContractError("shot_id is required")
        if type(self.revision) is not int or self.revision < 0:
            raise CandidateContractError("revision must be a non-negative integer")
        normalized = tuple(self.candidates)
        if len(normalized) < 3 or len(normalized) > 5:
            raise CandidateContractError("each shot must have 3 to 5 candidates")
        if any(not isinstance(item, CandidateEntry) for item in normalized):
            raise CandidateContractError("candidate set contains an unnormalized entry")
        ids = [item.candidate_id.casefold() for item in normalized]
        if len(ids) != len(set(ids)):
            raise CandidateContractError("candidate_id values must be unique")
        locked = [item for item in normalized if item.status == STATUS_LOCKED]
        if len(locked) > 1:
            raise CandidateContractError("a shot may have at most one locked candidate")
        # Do not let callers bypass the fail-closed factory by constructing a
        # ``CandidateSet`` directly.  This repeats the inexpensive structural
        # checks and the real-preview/provenance checks; no filesystem is
        # created or modified.
        _validate_normalized_rows(
            normalized,
            require_existing_preview=True,
            require_distinct_sources=True,
        )
        object.__setattr__(self, "candidates", normalized)

    @classmethod
    def from_candidates(
        cls,
        shot_id: str,
        candidates: Sequence[CandidateEntry | Mapping[str, Any]],
        *,
        require_existing_preview: bool = True,
        require_distinct_sources: bool = True,
    ) -> "CandidateSet":
        normalized = validate_candidate_set(
            shot_id,
            candidates,
            require_existing_preview=require_existing_preview,
            require_distinct_sources=require_distinct_sources,
        )
        return cls(shot_id=shot_id.strip(), candidates=normalized)

    @property
    def locked_candidate_id(self) -> str | None:
        for candidate in self.candidates:
            if candidate.status == STATUS_LOCKED:
                return candidate.candidate_id
        return None

    @property
    def locked_candidate(self) -> CandidateEntry | None:
        for candidate in self.candidates:
            if candidate.status == STATUS_LOCKED:
                return candidate
        return None

    def get(self, candidate_id: str) -> CandidateEntry:
        target = _candidate_id(candidate_id)
        for candidate in self.candidates:
            if candidate.candidate_id == target:
                return candidate
        raise CandidateNotFoundError(
            f"candidate {target!r} is not present in shot {self.shot_id!r}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "shot_id": self.shot_id,
            "revision": self.revision,
            "candidates": [item.to_dict() for item in self.candidates],
        }

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
        *,
        require_existing_preview: bool = True,
    ) -> "CandidateSet":
        if not isinstance(value, Mapping):
            raise CandidateContractError("candidate set must be an object")
        shot_id = value.get("shot_id")
        candidates = value.get("candidates")
        if not isinstance(shot_id, str) or not isinstance(candidates, Sequence) or isinstance(
            candidates, (str, bytes, bytearray)
        ):
            raise CandidateContractError("candidate set requires shot_id and candidates")
        result = cls.from_candidates(
            shot_id,
            candidates,
            require_existing_preview=require_existing_preview,
        )
        revision = value.get("revision", 0)
        if type(revision) is not int or revision < 0:
            raise CandidateContractError("revision must be a non-negative integer")
        return cls(result.shot_id, result.candidates, revision=revision)


MaterialCandidateSet = CandidateSet
CandidatePool = CandidateSet
CandidateState = CandidateSet


_ALIASES: dict[str, tuple[str, ...]] = {
    "candidate_id": ("candidate_id", "material_id", "source_id", "id"),
    "source": ("source", "source_name", "provider"),
    "preview_path": ("preview_path", "preview", "local_path", "save_path", "path"),
    "url": ("url", "source_url", "download_url"),
    "rights": ("rights", "license", "license_type", "license_status"),
    "evidence_kind": ("evidence_kind", "evidence", "kind"),
    "status": ("status", "candidate_status", "state"),
    "reason": ("reason", "matching_reason", "reject_reason", "selection_reason"),
    "media_validation": ("media_validation",),
    "rights_evidence": ("rights_evidence",),
}


def normalize_candidate(
    value: CandidateEntry | Mapping[str, Any],
) -> CandidateEntry:
    """Normalize one legacy or canonical mapping without inventing data.

    Missing required values raise ``CandidateContractError``.  In particular,
    this function never creates a placeholder ID, path, URL, rights label or
    preview file.
    """

    if isinstance(value, CandidateEntry):
        raw = value.to_dict()
    elif isinstance(value, Mapping):
        raw = value
    else:
        raise CandidateContractError("candidate must be an object")

    candidate_id = _candidate_id(_pick(raw, "candidate_id"))
    source = _required_text(_pick(raw, "source"), "source")
    preview_value = _pick(raw, "preview_path")
    if isinstance(preview_value, Path):
        preview_path = str(preview_value)
    else:
        preview_path = _required_text(preview_value, "preview_path")
    preview_path = preview_path.strip()
    if not preview_path:
        raise CandidateContractError("preview_path is required")
    if "://" in preview_path:
        raise CandidateContractError("preview_path must be a local path")

    url_value = _pick(raw, "url", default="")
    if url_value is None:
        url = ""
    elif isinstance(url_value, str):
        url = url_value.strip()
    else:
        raise CandidateContractError("url must be a string")
    if url and not _is_http_url(url):
        raise CandidateContractError("url must be an absolute http/https URL")

    rights = _required_text(_pick(raw, "rights"), "rights")
    evidence_kind = _normalise_evidence_kind(
        _required_text(_pick(raw, "evidence_kind"), "evidence_kind")
    )
    status = _normalise_status(_pick(raw, "status", default=STATUS_CANDIDATE))
    reason_value = _pick(raw, "reason", default="")
    if reason_value is None:
        reason = ""
    else:
        reason = _required_text(reason_value, "reason", allow_empty=True)
    media_validation = _normalise_media_validation(
        _pick(raw, "media_validation", default=None)
    )
    rights_evidence = _normalise_rights_evidence(
        _pick(raw, "rights_evidence", default=None)
    )

    return CandidateEntry(
        candidate_id=candidate_id,
        source=source,
        preview_path=preview_path,
        url=url,
        rights=rights,
        evidence_kind=evidence_kind,
        status=status,
        reason=reason,
        media_validation=media_validation,
        rights_evidence=rights_evidence,
    )


def normalize_candidate_entry(value: CandidateEntry | Mapping[str, Any]) -> CandidateEntry:
    """Descriptive alias for callers that prefer the full function name."""

    return normalize_candidate(value)


def normalize_candidates(
    candidates: Sequence[CandidateEntry | Mapping[str, Any]],
) -> tuple[CandidateEntry, ...]:
    if not isinstance(candidates, Sequence) or isinstance(
        candidates, (str, bytes, bytearray)
    ):
        raise CandidateContractError("candidates must be a list")
    return tuple(normalize_candidate(item) for item in candidates)


def validate_candidate_set(
    shot_id_or_candidates: str | Sequence[CandidateEntry | Mapping[str, Any]],
    candidates: Sequence[CandidateEntry | Mapping[str, Any]] | None = None,
    *,
    min_candidates: int = 3,
    max_candidates: int = 5,
    require_existing_preview: bool = True,
    require_distinct_sources: bool = True,
) -> tuple[CandidateEntry, ...]:
    """Validate and normalize the candidates for one shot.

    The two accepted call forms are ``validate_candidate_set(shot_id, rows)``
    and ``validate_candidate_set(rows)``.  The latter is useful for a caller
    that validates the rows before it knows the shot identity.  Validation is
    fail-closed and returns canonical rows only after every row passes.
    """

    if isinstance(shot_id_or_candidates, str):
        shot_id = shot_id_or_candidates.strip()
        raw_candidates = candidates
        if not shot_id:
            raise CandidateContractError("shot_id is required")
    else:
        shot_id = ""
        raw_candidates = shot_id_or_candidates
        if candidates is not None:
            raise CandidateContractError("candidates supplied twice")
    if raw_candidates is None or not isinstance(raw_candidates, Sequence) or isinstance(
        raw_candidates, (str, bytes, bytearray)
    ):
        raise CandidateContractError("candidates must be a list")
    if type(min_candidates) is not int or type(max_candidates) is not int:
        raise CandidateContractError("candidate bounds must be integers")
    if min_candidates < 1 or max_candidates < min_candidates:
        raise CandidateContractError("invalid candidate count bounds")
    count = len(raw_candidates)
    if count < min_candidates or count > max_candidates:
        raise CandidateContractError(
            f"each shot must have {min_candidates} to {max_candidates} candidates"
        )

    try:
        normalized = normalize_candidates(raw_candidates)
    except CandidateContractError:
        raise
    except Exception as exc:  # defensive boundary for foreign Mapping objects
        raise CandidateContractError(f"candidate normalization failed: {exc}") from exc

    _validate_normalized_rows(
        normalized,
        require_existing_preview=require_existing_preview,
        require_distinct_sources=require_distinct_sources,
    )
    # ``shot_id`` is intentionally only an identity check here; no state is
    # written and no production directory is inferred from it.
    _ = shot_id
    return normalized


def validate_candidate_pool(*args: Any, **kwargs: Any) -> tuple[CandidateEntry, ...]:
    """Alias retained for the product language used by the design docs."""

    return validate_candidate_set(*args, **kwargs)


def build_candidate_set(
    shot_id: str,
    candidates: Sequence[CandidateEntry | Mapping[str, Any]],
    **kwargs: Any,
) -> CandidateSet:
    return CandidateSet.from_candidates(shot_id, candidates, **kwargs)


def replace_candidate(
    state: CandidateSet,
    candidate_id: str | CandidateEntry | Mapping[str, Any],
    replacement: CandidateEntry | Mapping[str, Any] | None = None,
) -> CandidateSet:
    """Return a new set with one unlocked row replaced.

    A replacement may have a new ID, but it occupies the old row's position
    and cannot collide with another ID.  A locked row is never replaced.
    """

    _require_state(state)
    if replacement is None and not isinstance(candidate_id, str):
        replacement = candidate_id
        replacement_row = normalize_candidate(replacement)
        target_id = replacement_row.candidate_id
    else:
        target_id = _candidate_id(candidate_id)
        if replacement is None:
            raise CandidateContractError("replacement candidate is required")
        replacement_row = normalize_candidate(replacement)

    index = _index_for_id(state, target_id)
    current = state.candidates[index]
    if current.status == STATUS_LOCKED:
        raise CandidateLockedError(
            f"candidate {target_id!r} is locked and cannot be replaced"
        )
    if replacement_row.status == STATUS_LOCKED:
        raise CandidateContractError("replacement must not arrive already locked")
    if replacement_row.status == STATUS_REJECTED:
        raise CandidateContractError("replacement must be an active candidate")
    if replacement_row.candidate_id.casefold() != current.candidate_id.casefold():
        other_ids = {
            item.candidate_id.casefold()
            for item in state.candidates
            if item.candidate_id.casefold() != current.candidate_id.casefold()
        }
        if replacement_row.candidate_id.casefold() in other_ids:
            raise CandidateContractError("replacement candidate_id already exists")

    rows = list(state.candidates)
    rows[index] = replacement_row
    normalized = validate_candidate_set(
        state.shot_id,
        rows,
        require_existing_preview=True,
    )
    return CandidateSet(state.shot_id, normalized, revision=state.revision + 1)


def replace_candidates(
    state: CandidateSet,
    candidates: Sequence[CandidateEntry | Mapping[str, Any]],
) -> CandidateSet:
    """Replace a refreshed pool while preserving any existing lock.

    If a refresh omits or changes the locked row, the operation fails instead
    of silently changing the user's choice.  The lock is retained verbatim
    when the incoming row is equivalent.
    """

    _require_state(state)
    incoming = list(validate_candidate_set(state.shot_id, candidates))
    locked = state.locked_candidate
    if locked is not None:
        matching = next(
            (item for item in incoming if item.candidate_id == locked.candidate_id),
            None,
        )
        if matching is None:
            raise CandidateLockedError(
                f"refresh would remove locked candidate {locked.candidate_id!r}"
            )
        if matching != locked:
            raise CandidateLockedError(
                f"refresh would overwrite locked candidate {locked.candidate_id!r}"
            )
    return CandidateSet(state.shot_id, tuple(incoming), revision=state.revision + 1)


def lock_candidate(
    state: CandidateSet,
    candidate_id: str,
    *,
    reason: str = "",
) -> CandidateSet:
    """Return a new set with one active row locked."""

    _require_state(state)
    target_id = _candidate_id(candidate_id)
    index = _index_for_id(state, target_id)
    current = state.candidates[index]
    if current.status == STATUS_LOCKED:
        if reason.strip() and reason.strip() != current.reason:
            raise CandidateLockedError(
                f"candidate {target_id!r} is already locked"
            )
        return state
    if current.status == STATUS_REJECTED:
        raise CandidateContractError("a rejected candidate cannot be locked")
    if state.locked_candidate_id is not None:
        raise CandidateLockedError(
            f"shot {state.shot_id!r} already has locked candidate "
            f"{state.locked_candidate_id!r}"
        )
    lock_reason = reason.strip() or current.reason.strip()
    if not lock_reason:
        raise CandidateContractError("locking a candidate requires reason")
    # Re-run the fail-closed checks before granting the durable-looking state.
    validate_candidate_set(state.shot_id, state.candidates)
    rows = list(state.candidates)
    rows[index] = replace(current, status=STATUS_LOCKED, reason=lock_reason)
    return CandidateSet(state.shot_id, tuple(rows), revision=state.revision + 1)


def reselect_candidate(
    state: CandidateSet,
    candidate_id: str,
    *,
    reason: str,
) -> CandidateSet:
    """Explicitly move an existing lock to another active candidate.

    This is deliberately separate from ``replace_candidate``.  Ordinary
    replacement still cannot mutate a locked row; an operator must invoke this
    named transition and provide an audit reason.
    """

    _require_state(state)
    selection_reason = reason.strip() if isinstance(reason, str) else ""
    if not selection_reason:
        raise CandidateContractError("reselecting a candidate requires reason")
    previous_id = state.locked_candidate_id
    if previous_id is None:
        raise CandidateContractError("reselecting requires an existing locked candidate")
    target_id = _candidate_id(candidate_id)
    if target_id == previous_id:
        raise CandidateLockedError(f"candidate {target_id!r} is already locked")
    target_index = _index_for_id(state, target_id)
    target = state.candidates[target_index]
    if target.status == STATUS_REJECTED:
        raise CandidateContractError("a rejected candidate cannot be selected")
    previous_index = _index_for_id(state, previous_id)
    validate_candidate_set(state.shot_id, state.candidates)
    rows = list(state.candidates)
    rows[previous_index] = replace(rows[previous_index], status=STATUS_CANDIDATE)
    rows[target_index] = replace(
        target,
        status=STATUS_LOCKED,
        reason=selection_reason,
    )
    return CandidateSet(state.shot_id, tuple(rows), revision=state.revision + 1)


def reject_candidate(
    state: CandidateSet,
    candidate_id: str,
    *,
    reason: str,
) -> CandidateSet:
    """Return a new set with one unlocked row rejected for an explicit reason."""

    _require_state(state)
    rejection_reason = reason.strip() if isinstance(reason, str) else ""
    if not rejection_reason:
        raise CandidateContractError("rejecting a candidate requires reason")
    target_id = _candidate_id(candidate_id)
    index = _index_for_id(state, target_id)
    current = state.candidates[index]
    if current.status == STATUS_LOCKED:
        raise CandidateLockedError(
            f"candidate {target_id!r} is locked and cannot be rejected"
        )
    if current.status == STATUS_REJECTED and current.reason == rejection_reason:
        return state
    rows = list(state.candidates)
    rows[index] = replace(current, status=STATUS_REJECTED, reason=rejection_reason)
    return CandidateSet(state.shot_id, tuple(rows), revision=state.revision + 1)


def update_candidate_rights_evidence(
    state: CandidateSet,
    candidate_id: str,
    rights_evidence: Mapping[str, Any],
) -> CandidateSet:
    """Update only the per-use review evidence for an existing provider row.

    This named transition is allowed for a locked candidate because a rights
    review must not require unlocking or changing Ian's visual selection.
    Candidate identity, source, licence label, media and status remain bound
    by ``CandidateEntry`` validation.
    """

    _require_state(state)
    index = _index_for_id(state, candidate_id)
    current = state.candidates[index]
    if current.rights_evidence is None:
        raise CandidateContractError(
            "candidate has no structured stock rights evidence to review"
        )
    canonical = _normalise_rights_evidence(rights_evidence)
    if canonical is None:  # pragma: no cover - guarded by the typed argument
        raise CandidateContractError("rights_evidence is required")
    rows = list(state.candidates)
    rows[index] = replace(current, rights_evidence=canonical)
    return CandidateSet(state.shot_id, tuple(rows), revision=state.revision + 1)


def transition_candidate(
    state: CandidateSet,
    action: str,
    candidate_id: str,
    *,
    replacement: CandidateEntry | Mapping[str, Any] | None = None,
    reason: str = "",
) -> CandidateSet:
    """Dispatch one of the three pure state transitions."""

    action_name = action.strip().casefold() if isinstance(action, str) else ""
    if action_name == "replace":
        return replace_candidate(state, candidate_id, replacement)
    if action_name == "lock":
        return lock_candidate(state, candidate_id, reason=reason)
    if action_name == "reselect":
        return reselect_candidate(state, candidate_id, reason=reason)
    if action_name == "reject":
        return reject_candidate(state, candidate_id, reason=reason)
    raise CandidateContractError("action must be replace, lock, reselect, or reject")


# Explicit state-oriented aliases make the transition API discoverable without
# introducing mutable methods or a second state machine.
replace_candidate_state = replace_candidate
lock_candidate_state = lock_candidate
reselect_candidate_state = reselect_candidate
reject_candidate_state = reject_candidate
update_candidate_rights_evidence_state = update_candidate_rights_evidence


def _pick(raw: Mapping[str, Any], field_name: str, default: Any = None) -> Any:
    for key in _ALIASES[field_name]:
        if key in raw:
            return raw[key]
    return default


def _required_text(value: Any, field_name: str, *, allow_empty: bool = False) -> str:
    if isinstance(value, Path):
        text = str(value).strip()
    elif isinstance(value, str):
        text = value.strip()
    elif isinstance(value, int) and not isinstance(value, bool):
        text = str(value).strip()
    else:
        raise CandidateContractError(f"{field_name} must be a string")
    if not text and not allow_empty:
        raise CandidateContractError(f"{field_name} is required")
    if _CONTROL_CHARS.search(text):
        raise CandidateContractError(f"{field_name} contains control characters")
    return text


def _candidate_id(value: Any) -> str:
    candidate_id = _required_text(value, "candidate_id")
    # Candidate IDs are also public route segments.  Reject separators at the
    # contract boundary so a persisted row can never make serialization or
    # preview replacement produce an unsafe/invalid URL later.
    if "/" in candidate_id or "\\" in candidate_id:
        raise CandidateContractError("candidate_id must not contain path separators")
    return candidate_id


def _normalise_status(value: Any) -> CandidateStatus:
    text = _required_text(value, "status").casefold().replace("-", "_").replace(" ", "_")
    aliases: dict[str, CandidateStatus] = {
        "candidate": STATUS_CANDIDATE,
        "available": STATUS_CANDIDATE,
        "pending": STATUS_CANDIDATE,
        "proposed": STATUS_CANDIDATE,
        "locked": STATUS_LOCKED,
        "selected": STATUS_LOCKED,
        "rejected": STATUS_REJECTED,
        "reject": STATUS_REJECTED,
    }
    try:
        return aliases[text]
    except KeyError as exc:
        raise CandidateContractError(f"unsupported candidate status: {value!r}") from exc


def _normalise_evidence_kind(value: str) -> str:
    text = value.casefold().replace("-", "_").replace(" ", "_")
    aliases = {
        "direct": "direct_evidence",
        "direct_source": "direct_evidence",
        "self_owned_or_licensed": "licensed",
        "generated": "programmatic",
    }
    return aliases.get(text, text)


def _is_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme.casefold() in {"http", "https"} and bool(parsed.netloc)


def _rights_is_unknown(value: str) -> bool:
    normalized = " ".join(value.casefold().split())
    return normalized in _UNKNOWN_RIGHTS


def _normalise_media_validation(value: Any) -> dict[str, Any] | None:
    """Validate the safe technical report persisted by the admission service.

    A missing report remains valid for old projects.  New save/upload paths
    overwrite any caller-supplied value with a report computed from the actual
    bytes, while this structural check keeps malformed database payloads from
    reaching the browser or export gate.
    """

    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise CandidateContractError("media_validation must be an object")
    expected_fields = {
        "status",
        "media_kind",
        "mime_type",
        "sha256",
        "size_bytes",
        "width",
        "height",
        "duration_seconds",
        "codec",
        "container",
        "decode_verified",
    }
    if set(value) != expected_fields:
        raise CandidateContractError("media_validation fields are invalid")
    status = value.get("status")
    media_kind = value.get("media_kind")
    mime_type = value.get("mime_type")
    digest = value.get("sha256")
    if status != "verified" or value.get("decode_verified") is not True:
        raise CandidateContractError("media_validation is not verified")
    if media_kind not in {"image", "video"}:
        raise CandidateContractError("media_validation media_kind is invalid")
    allowed_mimes = (
        {"image/jpeg", "image/png", "image/webp"}
        if media_kind == "image"
        else {"video/mp4", "video/quicktime", "video/webm"}
    )
    if mime_type not in allowed_mimes:
        raise CandidateContractError("media_validation mime_type is invalid")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise CandidateContractError("media_validation sha256 is invalid")
    for field_name in ("size_bytes", "width", "height"):
        field_value = value.get(field_name)
        if type(field_value) is not int or field_value <= 0:
            raise CandidateContractError(
                f"media_validation {field_name} must be a positive integer"
            )
    duration = value.get("duration_seconds")
    if media_kind == "image":
        if duration is not None:
            raise CandidateContractError("image media_validation duration must be null")
    elif (
        isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or float(duration) <= 0
    ):
        raise CandidateContractError("video media_validation duration is invalid")
    for field_name in ("codec", "container"):
        field_value = value.get(field_name)
        if not isinstance(field_value, str) or _CONTROL_CHARS.search(field_value):
            raise CandidateContractError(
                f"media_validation {field_name} is invalid"
            )
    return {field_name: value[field_name] for field_name in expected_fields}


def _normalise_rights_evidence(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    try:
        return normalise_stock_rights_evidence(value)
    except StockRightsEvidenceError as exc:
        raise CandidateContractError(f"rights_evidence is invalid: {exc}") from exc


def _validate_normalized_rows(
    normalized: Sequence[CandidateEntry],
    *,
    require_existing_preview: bool,
    require_distinct_sources: bool,
) -> None:
    """Validate rows after normalization, including direct-construction paths."""

    ids = [item.candidate_id.casefold() for item in normalized]
    if len(ids) != len(set(ids)):
        raise CandidateContractError("candidate_id values must be unique")
    if require_distinct_sources and len({item.source.casefold() for item in normalized}) < 2:
        raise CandidateContractError("candidate set must contain more than one source")

    locked_count = 0
    usable_count = 0
    for item in normalized:
        if item.evidence_kind not in EVIDENCE_KINDS:
            raise CandidateContractError(
                f"candidate {item.candidate_id!r} has unsupported evidence_kind"
            )
        if _rights_is_unknown(item.rights):
            raise CandidateContractError(
                f"candidate {item.candidate_id!r} has unverified rights"
            )
        path = Path(item.preview_path)
        if not path.is_absolute():
            raise CandidateContractError(
                f"candidate {item.candidate_id!r} preview_path must be absolute"
            )
        if require_existing_preview:
            try:
                if not path.is_file():
                    raise CandidateContractError(
                        f"candidate {item.candidate_id!r} preview_path does not exist"
                    )
            except OSError as exc:
                raise CandidateContractError(
                    f"candidate {item.candidate_id!r} preview_path is unreadable"
                ) from exc
        if item.evidence_kind == "direct_evidence" and not item.url:
            raise CandidateContractError(
                f"candidate {item.candidate_id!r} direct_evidence requires url"
            )
        if item.status == STATUS_REJECTED:
            if not item.reason.strip():
                raise CandidateContractError(
                    f"rejected candidate {item.candidate_id!r} requires reason"
                )
        else:
            usable_count += 1
        if item.status == STATUS_LOCKED:
            locked_count += 1
            if not item.reason.strip():
                raise CandidateContractError(
                    f"locked candidate {item.candidate_id!r} requires reason"
                )
    if locked_count > 1:
        raise CandidateContractError("a shot may have at most one locked candidate")
    if usable_count == 0:
        raise CandidateContractError("candidate set has no usable candidate")


def _require_state(state: CandidateSet) -> None:
    if not isinstance(state, CandidateSet):
        raise CandidateContractError("state must be a CandidateSet")


def _index_for_id(state: CandidateSet, candidate_id: str) -> int:
    target = _candidate_id(candidate_id)
    for index, candidate in enumerate(state.candidates):
        if candidate.candidate_id == target:
            return index
    raise CandidateNotFoundError(
        f"candidate {target!r} is not present in shot {state.shot_id!r}"
    )


__all__ = [
    "CandidateContractError",
    "CandidateEntry",
    "CandidateLockedError",
    "CandidateMaterial",
    "CandidateNotFoundError",
    "CandidatePool",
    "CandidateSet",
    "CandidateState",
    "CandidateStatus",
    "EVIDENCE_KINDS",
    "MaterialCandidate",
    "MaterialCandidateSet",
    "STATUS_CANDIDATE",
    "STATUS_LOCKED",
    "STATUS_REJECTED",
    "build_candidate_set",
    "lock_candidate",
    "lock_candidate_state",
    "normalize_candidate",
    "normalize_candidate_entry",
    "normalize_candidates",
    "reselect_candidate",
    "reselect_candidate_state",
    "reject_candidate",
    "reject_candidate_state",
    "replace_candidate",
    "replace_candidate_state",
    "replace_candidates",
    "transition_candidate",
    "update_candidate_rights_evidence",
    "update_candidate_rights_evidence_state",
    "validate_candidate_pool",
    "validate_candidate_set",
]
