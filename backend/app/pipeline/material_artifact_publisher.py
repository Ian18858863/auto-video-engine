"""Fail-closed publication of worker artifacts.

The worker only owns an attempt payload.  This module is the parent-side
boundary that validates those files and publishes copies to the canonical and
project trees.  It intentionally keeps the database part behind a very small
fresh-read/compare-and-swap protocol so the filesystem tests can exercise the
same state machine without importing a web or ORM session.
"""

from __future__ import annotations

import base64
import copy
from dataclasses import dataclass
import hashlib
import json
import ntpath
import os
from pathlib import Path, PurePosixPath
import re
from typing import Any, Callable, Mapping, Protocol
from uuid import uuid4

from app.core.windows_durable_file import write_durable_bytes, write_durable_json
from app.core.windows_named_mutex import WindowsNamedMutex, build_mutex_name
from app.core.windows_storage_policy import (
    ApprovedPhysicalDirectory,
    approve_physical_d_write_root,
)
from app.core import windows_storage_policy as _storage_policy
from app.materials.asset_paths import AutoMaterialPaths
from app.materials.candidate_media_validation import (
    CandidateMediaLimits,
    CandidateMediaValidationError,
)
from app.materials.provenance import validate_execution_provenance
from app.pipeline.material_operation_protocol import (
    MaterialOperationRequest,
    MaterialWorkerResult,
    StagedArtifact,
    encode_material_worker_result,
)
from app.pipeline.material_runner_retirement import (
    MaterialRunnerRetirementCheckpoint,
    pending_runner_retirement_from_payload,
)


MAX_PUBLICATION_JOURNAL_BYTES = 2 * 1024 * 1024
MAX_PUBLICATION_ARTIFACT_BYTES = 4 * 1024 * 1024 * 1024
MAX_COLLISION_ATTEMPTS = 16
_IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp"})
_VIDEO_SUFFIXES = frozenset({".mp4", ".mov", ".webm"})
_MEDIA_MIME_BY_SUFFIX = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
}
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


class PublicationError(RuntimeError):
    """Base class for a rejected or incomplete publication."""


class PublicationRejected(PublicationError):
    """A pre-journal validation rejection with no retained publication effect."""


class PublicationCollision(PublicationError):
    """A bounded no-clobber destination search was exhausted."""


class PublicationAuditRequired(PublicationError):
    """The durable state is not safe to delete or retry automatically."""


class PublicationDatabase(Protocol):
    """Minimal DB seam used by the publisher.

    Implementations must return a fresh immutable snapshot and perform an
    exact whole-blob compare-and-swap.  Returning ``False`` means the expected
    blob was not current; raising after a write means the result is unknown and
    must be treated as an audit state.
    """

    def fresh_read(self) -> Mapping[str, Any]:
        ...

    def compare_and_swap(
        self,
        expected: Mapping[str, Any],
        replacement: Mapping[str, Any],
    ) -> bool:
        ...


@dataclass(frozen=True, slots=True)
class _FileIdentity:
    device: int
    inode: int


@dataclass(frozen=True, slots=True)
class _PreparedArtifact:
    artifact: StagedArtifact
    source_path: Path
    canonical_directory: Path
    project_directory: Path
    project_guard: Any
    project_targets: tuple[tuple[str, Path, Any], ...]
    filename: str
    media_validation: dict[str, Any] | None


@dataclass(slots=True)
class _PublicationMove:
    destination: Path
    part_path: Path
    sha256: str
    size_bytes: int
    part_identity: _FileIdentity
    target_kind: str
    ordinal: int
    target_label: str = ""
    owned: bool = True
    destination_identity: _FileIdentity | None = None
    state: str = "move_intent"

    def to_json(self) -> dict[str, Any]:
        return {
            "destination": str(self.destination),
            "part_path": str(self.part_path),
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "target_kind": self.target_kind,
            "ordinal": self.ordinal,
            "target_label": self.target_label,
            "owned": self.owned,
            "part_identity": {
                "device": self.part_identity.device,
                "inode": self.part_identity.inode,
            },
            "destination_identity": (
                None
                if self.destination_identity is None
                else {
                    "device": self.destination_identity.device,
                    "inode": self.destination_identity.inode,
                }
            ),
            "state": self.state,
        }


class CanonicalMaterialPublication:
    """A prepared publication with explicit tentative/committed states."""

    def __init__(
        self,
        *,
        publisher: "CanonicalMaterialArtifactPublisher",
        request: MaterialOperationRequest,
        attempt_dir: ApprovedPhysicalDirectory,
        prepared: tuple[_PreparedArtifact, ...],
        selected_index: int | None,
        expected_payload: Mapping[str, Any] | None,
        worker_result: MaterialWorkerResult,
    ) -> None:
        self.publisher = publisher
        self.request = request
        self.attempt_dir = attempt_dir
        self.prepared = prepared
        self.selected_index = selected_index
        self.expected_payload = (
            None if expected_payload is None else copy.deepcopy(dict(expected_payload))
        )
        if type(worker_result) is not MaterialWorkerResult:
            raise TypeError("worker_result must be MaterialWorkerResult")
        self.worker_result = worker_result
        self.moves: list[_PublicationMove] = []
        self.phase = "preparing"
        self._reference_committed = False
        self._released = False
        self._release_deferred = False
        self._payload_pin: ApprovedPhysicalDirectory | None = None
        self._result: Any | None = None
        self._journal_sha256: str | None = None
        self.journal: dict[str, Any] = {
            "format": "material-publication-v1",
            "execution_id": request.execution_id,
            "attempt_id": request.attempt_id,
            "operation": request.operation,
            "phase": "preparing",
            "moves": [],
            "registry": {},
            "completion_intent": None,
        }

    @property
    def result(self) -> Any | None:
        return self._result

    @property
    def completion_intent(self) -> dict[str, Any]:
        value = self.journal.get("completion_intent")
        if not isinstance(value, Mapping):
            raise PublicationAuditRequired(
                "publication has no durable completion intent"
            )
        return copy.deepcopy(dict(value))

    @property
    def ownership_retained(self) -> bool:
        """Whether publisher pins are intentionally held for runner retirement."""

        return self._release_deferred and not self._released

    @property
    def committed_journal_sha256(self) -> str:
        """Exact durable journal receipt used by DB completion and retirement."""

        if self.phase != "committed" or not self.ownership_retained:
            raise PublicationError(
                "committed journal receipt requires retained publication ownership"
            )
        value = self._journal_sha256
        if (
            type(value) is not str
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise PublicationAuditRequired(
                "committed publication has no exact durable journal receipt"
            )
        return value

    def assert_retained_ownership(self) -> None:
        """Prove publisher locks, pins, journal, and targets are still owned."""

        journal_sha256 = self.committed_journal_sha256
        if self._released or not self._release_deferred:
            raise PublicationAuditRequired(
                "committed publication ownership is no longer retained"
            )
        for pin in (
            self.attempt_dir,
            self._payload_pin,
            self.publisher._canonical_root_pin,
            self.publisher._registry_parent_pin,
        ):
            if pin is None:
                raise PublicationAuditRequired(
                    "retained publication is missing a required directory pin"
                )
            pin.verify()
        for item in self.prepared:
            for _label, _directory, guard in item.project_targets:
                verify = getattr(guard, "verify", None)
                if not callable(verify):
                    raise PublicationAuditRequired(
                        "retained publication has no project directory guard"
                    )
                verify()
        mutex = self.publisher.registry_mutex
        if mutex is None:
            raise PublicationAuditRequired(
                "retained publication has no registry mutex"
            )
        assert_owned = getattr(mutex, "assert_owned", None)
        if callable(assert_owned):
            try:
                assert_owned()
            except BaseException as exc:
                raise PublicationAuditRequired(
                    "retained publication registry mutex is not owned"
                ) from exc
        elif getattr(mutex, "acquired", None) is not True:
            raise PublicationAuditRequired(
                "retained publication registry mutex ownership is unknown"
            )
        self._assert_durable_committed_release_boundary()
        journal_path = self.attempt_dir.path / "publication-journal.json"
        try:
            journal_bytes = journal_path.read_bytes()
        except OSError as exc:
            raise PublicationAuditRequired(
                "retained publication journal cannot be read"
            ) from exc
        if hashlib.sha256(journal_bytes).hexdigest() != journal_sha256:
            raise PublicationAuditRequired(
                "retained publication journal changed after recovery"
            )
        self.publisher._verify_tentative_ownership(self)

    def __enter__(self) -> "CanonicalMaterialPublication":
        if self.phase not in {"preparing", "tentative"}:
            raise PublicationError(f"cannot enter publication from phase {self.phase}")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        _traceback: object,
    ) -> bool:
        if exc is None:
            return False
        if self.phase in {"preparing", "tentative"}:
            try:
                self.rollback()
            except PublicationAuditRequired:
                # rollback() has already persisted the audit state.  Keep the
                # original exception visible to the caller.
                pass
        else:
            self._mark_audit("publication_context_exception_after_reference")
        return False

    def publish_tentative(self) -> None:
        if self.phase == "tentative":
            return
        if self.phase != "preparing":
            raise PublicationError(f"cannot publish from phase {self.phase}")
        try:
            for index, item in enumerate(self.prepared):
                targets: tuple[tuple[str, str, Path, Any | None], ...] = (
                    ("canonical", "canonical", item.canonical_directory, None),
                    *tuple(
                        ("project", label, directory, guard)
                        for label, directory, guard in item.project_targets
                    ),
                )
                for target_kind, target_label, directory, target_guard in targets:
                    if target_kind == "project":
                        verify = getattr(target_guard, "verify", None)
                        if callable(verify):
                            verify()
                    if target_kind == "canonical":
                        move = self.publisher._reuse_duplicate_or_copy(
                            item,
                            attempt_id=self.request.attempt_id,
                            ordinal=index,
                            before_move=self._record_move_intent,
                            after_move=self._record_move_update,
                        )
                    else:
                        move = self.publisher._copy_with_collision(
                            item.source_path,
                            directory,
                            item.filename,
                            expected_sha256=item.artifact.sha256,
                            expected_size=item.artifact.size_bytes,
                            attempt_id=self.request.attempt_id,
                            ordinal=index,
                            target_kind=target_kind,
                            target_label=target_label,
                            before_move=self._record_move_intent,
                            after_move=self._record_move_update,
                        )
                    if target_kind == "project":
                        verify = getattr(target_guard, "verify", None)
                        if callable(verify):
                            verify()
            self.publisher._publish_registry(self)
            self.phase = "tentative"
            self.journal["phase"] = "tentative"
            self.publisher._write_journal(self)
            self._result = self.publisher._build_result(self)
            self.journal["completion_intent"] = self.publisher._build_completion_intent(
                self
            )
            # The completion intent must be durable before the reference CAS.
            # It is immutable across all later journal phases and is the only
            # restart-safe representation of the parent-built candidate batch.
            self.publisher._write_journal(self)
        except BaseException:
            # Keep all evidence.  The caller may explicitly invoke rollback;
            # an uncertain native move is never silently deleted here.
            self.journal["phase"] = self.phase
            try:
                self.publisher._write_journal(self)
            except BaseException:
                pass
            raise

    def _record_move_intent(self, move: _PublicationMove) -> None:
        self.moves.append(move)
        self.journal["moves"] = [item.to_json() for item in self.moves]
        self.publisher._write_journal(self)

    def _record_move_update(self, move: _PublicationMove) -> None:
        del move
        self.journal["moves"] = [item.to_json() for item in self.moves]
        self.publisher._write_journal(self)

    def commit(self, *, retain_ownership: bool = False) -> Any:
        if type(retain_ownership) is not bool:
            raise TypeError("retain_ownership must be a boolean")
        if self.phase == "committed":
            if retain_ownership and not self._released:
                self._release_deferred = True
            elif not retain_ownership and self.ownership_retained:
                self.release_after_retirement()
            return self._result
        if self.phase in {"db_committed", "audit_required"}:
            raise PublicationAuditRequired(
                "publication is already beyond the automatic rollback boundary"
            )
        self.publish_tentative()
        if self.expected_payload is not None and self.publisher.database is not None:
            expected = copy.deepcopy(self.expected_payload)
            try:
                current = _fresh_payload(self.publisher.database)
            except BaseException as exc:
                self._mark_audit("pre_reference_read_unknown")
                raise PublicationAuditRequired(
                    "database fresh read failed before reference CAS"
                ) from exc
            if current != expected:
                self._mark_audit("pre_reference_payload_changed")
                raise PublicationAuditRequired(
                    "database payload changed before publication reference CAS"
                )
            provisional = self.publisher._payload_with_phase(
                expected,
                phase="db_committed",
                execution_state="publishing",
                publication=self,
            )
            try:
                acknowledged = self.publisher.database.compare_and_swap(
                    expected,
                    provisional,
                )
            except BaseException as exc:
                self._mark_audit("reference_cas_unknown")
                raise PublicationAuditRequired(
                    "reference CAS acknowledgement is unknown"
                ) from exc
            if acknowledged is not True:
                try:
                    current_after = _fresh_payload(self.publisher.database)
                except BaseException as exc:
                    self._mark_audit("reference_cas_result_unknown")
                    raise PublicationAuditRequired(
                        "reference CAS result cannot be reconciled"
                    ) from exc
                if current_after == expected:
                    self.rollback()
                    raise PublicationError("reference CAS did not acquire the expected payload")
                self._mark_audit("reference_cas_conflict")
                raise PublicationAuditRequired(
                    "reference CAS lost ownership and cannot be rolled back"
                )
            self._reference_committed = True
            self.phase = "db_committed"
            self.journal["phase"] = "db_committed"
            self.publisher._write_journal(self)
            try:
                self.publisher._verify_tentative_ownership(self)
            except BaseException as exc:
                # The reference may already be durable.  Never leave the
                # in-memory phase at db_committed while presenting a normal
                # exception to the caller; the only safe next action is an
                # explicit audit/recovery decision.
                self._mark_audit("tentative_attestation_failed")
                raise PublicationAuditRequired(
                    "tentative publication attestation failed after reference CAS"
                ) from exc
            # This is the durable attestation boundary.  Completion CAS is
            # not attempted until the journal records that every published
            # identity/hash and the registry snapshot were re-verified.
            self.journal["attestation"] = "committed"
            self.publisher._write_journal(self)
            if not retain_ownership:
                # Compatibility callers still receive the historical two-CAS
                # publication.  The typed coordinator retains ownership and
                # deliberately defers this final transition so its lifecycle
                # adapter can atomically write task result, attempt history,
                # next-task selection and active-owner clear in one fresh CAS.
                final_payload = self.publisher._payload_with_phase(
                    provisional,
                    phase="committed",
                    execution_state="finished",
                    publication=self,
                )
                try:
                    completed = self.publisher.database.compare_and_swap(
                        provisional,
                        final_payload,
                    )
                except BaseException as exc:
                    self._mark_audit("completion_cas_unknown")
                    raise PublicationAuditRequired(
                        "completion CAS acknowledgement is unknown"
                    ) from exc
                if completed is not True:
                    self._mark_audit("completion_cas_conflict")
                    raise PublicationAuditRequired(
                        "completion CAS failed after database references were committed"
                    )
        self.phase = "committed"
        self.journal["phase"] = "committed"
        self.publisher._write_journal(self)
        self._release_deferred = retain_ownership
        if not retain_ownership:
            self.publisher._release(self)
        return self._result

    def release_after_retirement(self) -> None:
        """Release publisher ownership after a runner retirement proof.

        A committed publication may be retained while the runner proves its
        own termination and retirement.  The durable journal is re-read before
        any pin or mutex is closed so a caller cannot release ownership from a
        stale in-memory phase.
        """

        if self._released:
            return
        if self.phase != "committed":
            raise PublicationError(
                "publisher ownership can be released only after commit"
            )
        self._assert_durable_committed_release_boundary()
        self.publisher._release(self)
        self._release_deferred = False

    def _assert_durable_committed_release_boundary(self) -> None:
        journal_path = self.attempt_dir.path / "publication-journal.json"
        try:
            metadata = journal_path.lstat()
            attributes = int(getattr(metadata, "st_file_attributes", 0))
            if journal_path.is_symlink() or attributes & 0x0400:
                raise PublicationAuditRequired(
                    "publication journal is a reparse point"
                )
            if not journal_path.is_file() or metadata.st_nlink != 1:
                raise PublicationAuditRequired(
                    "publication journal is not an owned regular file"
                )
            if metadata.st_size > MAX_PUBLICATION_JOURNAL_BYTES:
                raise PublicationAuditRequired(
                    "publication journal exceeds the size limit"
                )
            raw = json.loads(journal_path.read_text(encoding="utf-8"))
        except PublicationAuditRequired as exc:
            self._mark_audit_preserving_journal("release_journal_invalid")
            raise PublicationAuditRequired(
                "durable publication journal cannot prove release ownership"
            ) from exc
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._mark_audit_preserving_journal("release_journal_unreadable")
            raise PublicationAuditRequired(
                "durable publication journal cannot be reconciled for release"
            ) from exc
        if not isinstance(raw, dict):
            self._mark_audit_preserving_journal("release_journal_not_object")
            raise PublicationAuditRequired(
                "durable publication journal is not an object"
            )
        if (
            raw.get("format") != "material-publication-v1"
            or raw.get("execution_id") != self.request.execution_id
            or raw.get("attempt_id") != self.request.attempt_id
            or raw.get("operation") != self.request.operation
            or raw.get("phase") != "committed"
            or not isinstance(raw.get("registry"), dict)
            or not isinstance(raw.get("moves"), list)
        ):
            self._mark_audit_preserving_journal("release_journal_identity_or_phase")
            raise PublicationAuditRequired(
                "durable publication journal identity or phase changed"
            )
        if self.expected_payload is not None and raw.get("attestation") != "committed":
            self._mark_audit_preserving_journal("release_journal_attestation_missing")
            raise PublicationAuditRequired(
                "durable publication journal lacks committed attestation"
            )
        if (
            raw.get("registry") != self.journal.get("registry", {})
            or raw.get("moves") != [move.to_json() for move in self.moves]
            or raw.get("completion_intent")
            != self.journal.get("completion_intent")
        ):
            self._mark_audit_preserving_journal("release_journal_evidence_mismatch")
            raise PublicationAuditRequired(
                "durable publication journal evidence changed before release"
            )

    def rollback(self) -> None:
        if self.phase in {"db_committed", "committed", "audit_required"} or self._reference_committed:
            raise PublicationAuditRequired(
                "rollback is forbidden after a database reference may have committed"
            )
        # The in-memory phase is only a hint.  A caller can be interrupted
        # between a durable journal write and the exception it observes, so a
        # delete-capable rollback must first prove that the on-disk journal is
        # still in the pre-reference boundary and agrees with every move we
        # intend to inspect.
        self._assert_durable_rollback_boundary()
        if self.publisher.database is not None and self.expected_payload is not None:
            try:
                current = _fresh_payload(self.publisher.database)
            except BaseException as exc:
                self._mark_audit("rollback_read_unknown")
                raise PublicationAuditRequired(
                    "database fresh read failed before rollback"
                ) from exc
            if current != self.expected_payload:
                self._mark_audit("rollback_payload_not_pre_reference")
                raise PublicationAuditRequired(
                    "fresh database read does not prove the pre-reference payload"
                )
        try:
            # Restore the registry first.  If that proof fails, no published
            # file has been deleted and the entire tree remains auditable.
            self.publisher._restore_registry(self)
            for move in reversed(self.moves):
                if move.state == "move_applied":
                    current_identity = _identity(move.destination)
                    if current_identity != move.destination_identity:
                        self._mark_audit("rollback_destination_identity_changed")
                        raise PublicationAuditRequired(
                            "published destination identity changed before rollback"
                        )
                    _remove_exact_part(
                        move.destination,
                        expected_identity=move.destination_identity,
                        expected_sha256=move.sha256,
                        expected_size=move.size_bytes,
                    )
                elif move.state == "move_intent":
                    if move.destination.exists():
                        destination_identity = _identity(move.destination)
                        if destination_identity != move.part_identity:
                            self._mark_audit("rollback_intent_destination_unknown")
                            raise PublicationAuditRequired(
                                "intent-only destination identity is not the part identity"
                            )
                        _remove_exact_part(
                            move.destination,
                            expected_identity=destination_identity,
                            expected_sha256=move.sha256,
                            expected_size=move.size_bytes,
                        )
                    elif move.part_path.exists():
                        _remove_exact_part(
                            move.part_path,
                            expected_identity=move.part_identity,
                            expected_sha256=move.sha256,
                            expected_size=move.size_bytes,
                        )
                    else:
                        self._mark_audit("rollback_intent_evidence_missing")
                        raise PublicationAuditRequired(
                            "intent-only part and destination evidence are both missing"
                        )
                elif move.state == "move_applied_unconfirmed":
                    self._mark_audit("move_applied_journal_unknown")
                    raise PublicationAuditRequired(
                        "move applied state is not durably attested"
                    )
                elif move.state == "collision_unconfirmed":
                    self._mark_audit("collision_journal_unknown")
                    raise PublicationAuditRequired(
                        "collision state is not durably attested"
                    )
            self.phase = "rolled_back"
            self.journal["phase"] = "rolled_back"
            self.journal["attestation"] = "rolled_back"
            self.publisher._write_journal(self)
            self.publisher._release(self)
        except PublicationAuditRequired:
            raise
        except BaseException as exc:
            self._mark_audit("rollback_delete_unknown")
            raise PublicationAuditRequired(
                "rollback could not prove exact file ownership"
            ) from exc

    def _assert_durable_rollback_boundary(self) -> None:
        journal_path = self.attempt_dir.path / "publication-journal.json"
        try:
            metadata = journal_path.lstat()
            attributes = int(getattr(metadata, "st_file_attributes", 0))
            if journal_path.is_symlink() or attributes & 0x0400:
                raise PublicationAuditRequired("publication journal is a reparse point")
            if not journal_path.is_file() or metadata.st_nlink != 1:
                raise PublicationAuditRequired("publication journal is not an owned regular file")
            if metadata.st_size > MAX_PUBLICATION_JOURNAL_BYTES:
                raise PublicationAuditRequired("publication journal exceeds the size limit")
            raw = json.loads(journal_path.read_text(encoding="utf-8"))
        except PublicationAuditRequired as exc:
            self._mark_audit_preserving_journal("rollback_journal_invalid")
            raise PublicationAuditRequired(
                "durable publication journal cannot prove rollback ownership"
            ) from exc
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._mark_audit_preserving_journal("rollback_journal_unreadable")
            raise PublicationAuditRequired(
                "durable publication journal cannot be reconciled"
            ) from exc
        if not isinstance(raw, dict):
            self._mark_audit_preserving_journal("rollback_journal_not_object")
            raise PublicationAuditRequired("durable publication journal is not an object")
        if (
            raw.get("execution_id") != self.request.execution_id
            or raw.get("attempt_id") != self.request.attempt_id
        ):
            self._mark_audit_preserving_journal("rollback_journal_identity_mismatch")
            raise PublicationAuditRequired("durable publication journal identity changed")
        if (
            raw.get("format") != "material-publication-v1"
            or raw.get("operation") != self.request.operation
            or not isinstance(raw.get("registry"), dict)
            or not isinstance(raw.get("moves"), list)
        ):
            self._mark_audit_preserving_journal("rollback_journal_schema_invalid")
            raise PublicationAuditRequired("durable publication journal schema is invalid")
        if (
            raw.get("registry") != self.journal.get("registry", {})
            or raw.get("attestation") != self.journal.get("attestation")
            or raw.get("completion_intent")
            != self.journal.get("completion_intent")
        ):
            self._mark_audit_preserving_journal("rollback_journal_control_plane_mismatch")
            raise PublicationAuditRequired(
                "durable publication journal control-plane evidence changed"
            )
        durable_phase = raw.get("phase")
        if durable_phase not in {"preparing", "tentative"} or durable_phase != self.phase:
            self._mark_audit_preserving_journal("rollback_journal_phase_not_pre_reference")
            raise PublicationAuditRequired(
                "durable publication journal is beyond the rollback boundary"
            )
        durable_moves = raw.get("moves")
        expected_moves = [move.to_json() for move in self.moves]
        if durable_moves != expected_moves:
            self._mark_audit_preserving_journal("rollback_journal_moves_mismatch")
            raise PublicationAuditRequired(
                "durable publication journal does not match in-memory move evidence"
            )

    def _mark_audit_preserving_journal(self, reason: str) -> None:
        """Record an audit sidecar without replacing questionable evidence."""

        self.phase = "audit_required"
        self.journal["phase"] = "audit_required"
        self.journal["reason_code"] = reason
        marker = {
            "format": "material-publication-audit-v1",
            "execution_id": self.request.execution_id,
            "attempt_id": self.request.attempt_id,
            "reason_code": reason,
        }
        try:
            write_durable_json(
                parent=self.attempt_dir,
                filename="publication-audit.json",
                payload=marker,
                replace=False,
                max_bytes=MAX_PUBLICATION_JOURNAL_BYTES,
            )
        except BaseException:
            # The original journal remains untouched even if the sidecar
            # cannot be created; the caller still receives an audit state.
            pass

    def _mark_audit(self, reason: str) -> None:
        self.phase = "audit_required"
        self.journal["phase"] = "audit_required"
        self.journal["reason_code"] = reason
        try:
            self.publisher._write_journal(self)
        except BaseException:
            pass


class CanonicalMaterialArtifactPublisher:
    """Validate and publish one worker result under a registry mutex."""

    def __init__(
        self,
        *,
        canonical_paths: AutoMaterialPaths,
        database: PublicationDatabase | None = None,
        expected_payload: Mapping[str, Any] | None = None,
        registry_path: Path | None = None,
        registry_mutex: Any | None = None,
        max_artifact_bytes: int = MAX_PUBLICATION_ARTIFACT_BYTES,
        media_validator: Callable[..., Any] | None = None,
    ) -> None:
        if type(canonical_paths) is not AutoMaterialPaths:
            raise TypeError("canonical_paths must be AutoMaterialPaths")
        if not isinstance(max_artifact_bytes, int) or isinstance(max_artifact_bytes, bool):
            raise TypeError("max_artifact_bytes must be an integer")
        if max_artifact_bytes <= 0:
            raise ValueError("max_artifact_bytes must be positive")
        if media_validator is not None and not callable(media_validator):
            raise TypeError("media_validator must be callable")
        if (database is None) != (expected_payload is None):
            raise ValueError(
                "database and expected_payload must be supplied together; "
                "publication CAS cannot be skipped implicitly"
            )
        self.canonical_paths = canonical_paths
        self.database = database
        self.expected_payload = (
            None if expected_payload is None else copy.deepcopy(dict(expected_payload))
        )
        self.registry_path = registry_path or canonical_paths.fingerprint_registry_path
        self.registry_mutex = registry_mutex
        self.max_artifact_bytes = max_artifact_bytes
        self.media_validator = media_validator
        self._registry_parent_pin: ApprovedPhysicalDirectory | None = None
        self._canonical_root_pin: ApprovedPhysicalDirectory | None = None
        self._old_registry_bytes: bytes | None = None
        self._new_registry_sha256: str | None = None

    def prepare(
        self,
        request: MaterialOperationRequest,
        attempt_dir: ApprovedPhysicalDirectory,
        worker_result: MaterialWorkerResult,
        *,
        project_directory_uses: Mapping[str, Any],
    ) -> CanonicalMaterialPublication:
        if type(request) is not MaterialOperationRequest:
            raise TypeError("request must be MaterialOperationRequest")
        if not isinstance(attempt_dir, ApprovedPhysicalDirectory):
            raise TypeError("attempt_dir must be an ApprovedPhysicalDirectory")
        if type(worker_result) is not MaterialWorkerResult:
            raise TypeError("worker_result must be a MaterialWorkerResult")
        if worker_result.status != "succeeded":
            raise PublicationError("only a succeeded worker result can be published")
        if request.execution_id != worker_result.execution_id or request.attempt_id != worker_result.attempt_id:
            raise PublicationError("worker result identity does not match request")
        if worker_result.operation != request.operation:
            raise PublicationError("worker result operation does not match request")
        attempt_dir.verify()
        attempt_pin = approve_physical_d_write_root(attempt_dir.path)
        payload_root = attempt_pin.path / "payload"
        payload_pin: ApprovedPhysicalDirectory | None = None
        try:
            payload_pin = approve_physical_d_write_root(payload_root)
            canonical_root = _require_d_directory(self.canonical_paths.root)
            self._canonical_root_pin = approve_physical_d_write_root(canonical_root)
            _validate_canonical_directories(self.canonical_paths)
            self._acquire_registry_mutex()
            prepared: list[_PreparedArtifact] = []
            seen_relative_paths: set[str] = set()
            seen_fingerprints: set[str] = set()
            for artifact in worker_result.artifacts:
                relative_key = artifact.relative_path.casefold()
                if relative_key in seen_relative_paths:
                    raise PublicationError("worker result contains duplicate artifact paths")
                if artifact.sha256 in seen_fingerprints:
                    raise PublicationError(
                        "worker result contains duplicate artifact fingerprints"
                    )
                seen_relative_paths.add(relative_key)
                seen_fingerprints.add(artifact.sha256)
                source = _validate_staged_artifact(
                    artifact,
                    payload_root=payload_root,
                    max_bytes=self.max_artifact_bytes,
                )
                canonical_directory = self._canonical_directory(request, artifact)
                media_validation = self._validate_media_content(source, artifact)
                if request.operation == "stock_candidates" and media_validation is None:
                    raise PublicationError(
                        "stock candidate publication requires parent media validation"
                    )
                project_targets = self._project_directory(
                    request,
                    artifact,
                    project_directory_uses,
                )
                project_directory, project_guard = project_targets[0][1:]
                filename = _safe_filename(source.name)
                prepared.append(
                    _PreparedArtifact(
                        artifact=artifact,
                        source_path=source,
                        canonical_directory=canonical_directory,
                        project_directory=project_directory,
                        project_guard=project_guard,
                        project_targets=tuple(project_targets),
                        filename=filename,
                        media_validation=media_validation,
                    )
                )
            _validate_declared_payload_files(
                payload_root,
                tuple(item.source_path for item in prepared),
            )
            selected_index = _selected_index(worker_result)
            publication = CanonicalMaterialPublication(
                publisher=self,
                request=request,
                attempt_dir=attempt_pin,
                prepared=tuple(prepared),
                selected_index=selected_index,
                expected_payload=self.expected_payload,
                worker_result=worker_result,
            )
            publication._payload_pin = payload_pin
            payload_pin = None  # type: ignore[assignment]
            self._reject_existing_journal(publication.attempt_dir)
            self._write_journal(publication, create=True)
            attempt_pin = None  # type: ignore[assignment]
            return publication
        except BaseException:
            if payload_pin is not None:
                payload_pin.close()
            if attempt_pin is not None:
                attempt_pin.close()
            if self._canonical_root_pin is not None:
                self._canonical_root_pin.close()
                self._canonical_root_pin = None
            self._release_mutex_only()
            raise

    def adopt_committed_publication(
        self,
        request: MaterialOperationRequest,
        attempt_dir: ApprovedPhysicalDirectory,
        worker_result: MaterialWorkerResult,
        *,
        project_directory_uses: Mapping[str, Any],
    ) -> CanonicalMaterialPublication:
        return self._adopt_referenced_publication(
            request,
            attempt_dir,
            worker_result,
            project_directory_uses=project_directory_uses,
            completed_checkpoint=None,
        )

    def adopt_completed_publication(
        self,
        request: MaterialOperationRequest,
        attempt_dir: ApprovedPhysicalDirectory,
        worker_result: MaterialWorkerResult,
        *,
        project_directory_uses: Mapping[str, Any],
    ) -> CanonicalMaterialPublication:
        """Re-attest a final pending success without replaying publication."""

        if self.expected_payload is None:
            raise PublicationAuditRequired(
                "completed publication adoption requires an expected payload"
            )
        try:
            checkpoint = pending_runner_retirement_from_payload(
                self.expected_payload
            )
        except ValueError as exc:
            raise PublicationAuditRequired(
                "completed publication has no exact pending retirement checkpoint"
            ) from exc
        if checkpoint.completion_status != "succeeded":
            raise PublicationAuditRequired(
                "only a successful pending completion owns a publication"
            )
        return self._adopt_referenced_publication(
            request,
            attempt_dir,
            worker_result,
            project_directory_uses=project_directory_uses,
            completed_checkpoint=checkpoint,
        )

    def _adopt_referenced_publication(
        self,
        request: MaterialOperationRequest,
        attempt_dir: ApprovedPhysicalDirectory,
        worker_result: MaterialWorkerResult,
        *,
        project_directory_uses: Mapping[str, Any],
        completed_checkpoint: MaterialRunnerRetirementCheckpoint | None,
    ) -> CanonicalMaterialPublication:
        """Re-adopt a referenced publication without copying or replacing it.

        The caller must already own the material-step recovery mutex through
        the claim-bound ``database`` seam.  This method only reopens/pins the
        attempt, canonical and project roots; revalidates the durable worker
        manifest, journal, published targets and registry; and reconstructs the
        parent result from the immutable completion intent.  It never calls a
        copy, collision or registry replacement path.
        """

        if self.database is None or self.expected_payload is None:
            raise PublicationAuditRequired(
                "restart publication adoption requires a claim-bound database"
            )
        if type(request) is not MaterialOperationRequest:
            raise TypeError("request must be MaterialOperationRequest")
        if not isinstance(attempt_dir, ApprovedPhysicalDirectory):
            raise TypeError("attempt_dir must be an ApprovedPhysicalDirectory")
        if type(worker_result) is not MaterialWorkerResult:
            raise TypeError("worker_result must be MaterialWorkerResult")
        if worker_result.status != "succeeded":
            raise PublicationAuditRequired(
                "restart publication adoption requires a successful worker result"
            )
        if (
            worker_result.execution_id != request.execution_id
            or worker_result.attempt_id != request.attempt_id
            or worker_result.operation != request.operation
        ):
            raise PublicationAuditRequired(
                "restart worker result identity does not match the request"
            )
        current = _fresh_payload(self.database)
        if current != self.expected_payload:
            raise PublicationAuditRequired(
                "restart publication expected payload is not current"
            )
        if completed_checkpoint is None:
            _validate_referenced_publishing_payload(
                current,
                request,
                attempt_dir.path,
            )
        else:
            _validate_referenced_completed_payload(
                current,
                request,
                attempt_dir.path,
                checkpoint=completed_checkpoint,
            )

        attempt_dir.verify()
        attempt_pin = approve_physical_d_write_root(attempt_dir.path)
        payload_pin: ApprovedPhysicalDirectory | None = None
        try:
            payload_root = attempt_pin.path / "payload"
            payload_pin = approve_physical_d_write_root(payload_root)
            canonical_root = _require_d_directory(self.canonical_paths.root)
            self._canonical_root_pin = approve_physical_d_write_root(canonical_root)
            _validate_canonical_directories(self.canonical_paths)
            self._acquire_registry_mutex()

            prepared: list[_PreparedArtifact] = []
            seen_relative_paths: set[str] = set()
            seen_fingerprints: set[str] = set()
            for artifact in worker_result.artifacts:
                relative_key = artifact.relative_path.casefold()
                if relative_key in seen_relative_paths or artifact.sha256 in seen_fingerprints:
                    raise PublicationAuditRequired(
                        "restart worker manifest contains duplicate artifacts"
                    )
                seen_relative_paths.add(relative_key)
                seen_fingerprints.add(artifact.sha256)
                source = _validate_staged_artifact(
                    artifact,
                    payload_root=payload_root,
                    max_bytes=self.max_artifact_bytes,
                )
                canonical_directory = self._canonical_directory(request, artifact)
                media_validation = self._validate_media_content(source, artifact)
                if request.operation == "stock_candidates" and media_validation is None:
                    raise PublicationAuditRequired(
                        "stock recovery requires parent media validation"
                    )
                project_targets = self._project_directory(
                    request,
                    artifact,
                    project_directory_uses,
                )
                project_directory, project_guard = project_targets[0][1:]
                prepared.append(
                    _PreparedArtifact(
                        artifact=artifact,
                        source_path=source,
                        canonical_directory=canonical_directory,
                        project_directory=project_directory,
                        project_guard=project_guard,
                        project_targets=tuple(project_targets),
                        filename=_safe_filename(source.name),
                        media_validation=media_validation,
                    )
                )
            _validate_declared_payload_files(
                payload_root,
                tuple(item.source_path for item in prepared),
            )
            publication = CanonicalMaterialPublication(
                publisher=self,
                request=request,
                attempt_dir=attempt_pin,
                prepared=tuple(prepared),
                selected_index=_selected_index(worker_result),
                expected_payload=self.expected_payload,
                worker_result=worker_result,
            )

            journal, journal_sha256 = _read_recovery_publication_journal(
                publication.attempt_dir.path / "publication-journal.json"
            )
            publication.journal = journal
            publication.moves = _recover_publication_moves(publication)
            self._verify_tentative_ownership(publication)
            publication._result = self._build_result(publication)
            _validate_recovery_completion_intent(
                publication,
                expected_payload=current,
                completed_checkpoint=completed_checkpoint,
            )

            durable_phase = journal.get("phase")
            if durable_phase == "committed":
                if journal.get("attestation") != "committed":
                    raise PublicationAuditRequired(
                        "committed restart journal has no committed attestation"
                    )
                publication._journal_sha256 = journal_sha256
            elif (
                completed_checkpoint is None
                and durable_phase in {"tentative", "db_committed"}
            ):
                if journal.get("attestation") not in (None, "committed"):
                    raise PublicationAuditRequired(
                        "referenced restart journal has an invalid attestation"
                    )
                # Runner adoption must prove the old controller/root quiescent
                # before a caller reaches this method.  With the same recovery
                # mutex and all target locks now held, this fresh attestation may
                # safely seal an interrupted reference publication.
                publication.journal["phase"] = "committed"
                publication.journal["attestation"] = "committed"
                self._write_journal(publication)
            else:
                raise PublicationAuditRequired(
                    "restart journal is not at a referenced recoverable phase"
                )

            publication.phase = "committed"
            publication._reference_committed = True
            publication._release_deferred = True
            publication._payload_pin = payload_pin
            payload_pin = None
            attempt_pin = None
            return publication
        except BaseException:
            if payload_pin is not None:
                payload_pin.close()
            if attempt_pin is not None:
                attempt_pin.close()
            if self._canonical_root_pin is not None:
                self._canonical_root_pin.close()
                self._canonical_root_pin = None
            self._release_mutex_only()
            raise

    def _validate_media_content(
        self,
        source: Path,
        artifact: StagedArtifact,
    ) -> dict[str, Any] | None:
        validator = self.media_validator
        if validator is None:
            return None
        try:
            report = validator(
                source,
                declared_kind=artifact.media_kind,
                declared_mime=_MEDIA_MIME_BY_SUFFIX[source.suffix.casefold()],
                limits=CandidateMediaLimits(max_bytes=self.max_artifact_bytes),
            )
        except CandidateMediaValidationError as exc:
            raise PublicationRejected(
                f"staged artifact media validation failed [{exc.code}]: {exc}"
            ) from exc
        except BaseException as exc:
            raise PublicationError("staged artifact media validation failed") from exc
        if (
            getattr(report, "media_kind", None) != artifact.media_kind
            or getattr(report, "sha256", None) != artifact.sha256
            or getattr(report, "size_bytes", None) != artifact.size_bytes
            or getattr(report, "decode_verified", None) is not True
        ):
            raise PublicationError(
                "staged artifact media report does not match worker declaration"
            )
        to_dict = getattr(report, "to_dict", None)
        if not callable(to_dict):
            raise PublicationError(
                "staged artifact media validator returned no serializable report"
            )
        value = to_dict()
        if not isinstance(value, Mapping):
            raise PublicationError(
                "staged artifact media validator returned an invalid report"
            )
        return copy.deepcopy(dict(value))

    def _acquire_registry_mutex(self) -> None:
        if self.registry_mutex is None:
            name = build_mutex_name("asset-registry", str(self.registry_path.resolve()))
            self.registry_mutex = WindowsNamedMutex.try_acquire(name)
        elif hasattr(self.registry_mutex, "acquire"):
            # A test seam may provide an unowned lock object.
            acquired = getattr(self.registry_mutex, "acquired", None)
            if acquired is not True:
                try:
                    self.registry_mutex.acquire()
                except TypeError:
                    self.registry_mutex.acquire(timeout_ms=0)
        self._registry_parent_pin = approve_physical_d_write_root(self.registry_path.parent)
        _validate_registry_file(self.registry_path)

    def _release_mutex_only(self) -> None:
        pin = self._registry_parent_pin
        self._registry_parent_pin = None
        if pin is not None:
            pin.close()
        canonical_pin = self._canonical_root_pin
        self._canonical_root_pin = None
        if canonical_pin is not None:
            canonical_pin.close()
        mutex = self.registry_mutex
        self.registry_mutex = None
        if mutex is not None:
            release = getattr(mutex, "release", None)
            if callable(release):
                release()
            else:
                close = getattr(mutex, "close", None)
                if callable(close):
                    close()

    def _release(self, publication: CanonicalMaterialPublication) -> None:
        if publication._released:
            return
        publication._released = True
        payload_pin = getattr(publication, "_payload_pin", None)
        if payload_pin is not None:
            payload_pin.close()
        publication.attempt_dir.close()
        self._release_mutex_only()

    def _write_journal(
        self,
        publication: CanonicalMaterialPublication,
        *,
        create: bool = False,
    ) -> None:
        publication.journal["moves"] = [move.to_json() for move in publication.moves]
        receipt = write_durable_json(
            parent=publication.attempt_dir,
            filename="publication-journal.json",
            payload=publication.journal,
            replace=not create,
            max_bytes=MAX_PUBLICATION_JOURNAL_BYTES,
        )
        publication._journal_sha256 = receipt.sha256

    @staticmethod
    def _reject_existing_journal(attempt_dir: ApprovedPhysicalDirectory) -> None:
        journal_path = attempt_dir.path / "publication-journal.json"
        try:
            metadata = journal_path.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise PublicationAuditRequired(
                "publication journal cannot be inspected"
            ) from exc
        if journal_path.is_symlink() or int(getattr(metadata, "st_file_attributes", 0)) & 0x0400:
            raise PublicationAuditRequired(
                "existing publication journal is a reparse point"
            )
        if not journal_path.is_file() or metadata.st_nlink != 1:
            raise PublicationAuditRequired(
                "existing publication journal is not an owned regular file"
            )
        raise PublicationAuditRequired(
            "existing publication journal requires explicit recovery"
        )

    def _copy_with_collision(
        self,
        source: Path,
        directory: Path,
        filename: str,
        *,
        expected_sha256: str,
        expected_size: int,
        attempt_id: str,
        ordinal: int,
        target_kind: str,
        target_label: str = "",
        before_move: Callable[[_PublicationMove], None] | None = None,
        after_move: Callable[[_PublicationMove], None] | None = None,
    ) -> _PublicationMove:
        directory = _require_d_directory(directory)
        parent_pin = approve_physical_d_write_root(directory)
        parent_pin.close()
        for attempt in range(MAX_COLLISION_ATTEMPTS):
            candidate = _collision_name(
                filename,
                attempt_id=attempt_id,
                ordinal=ordinal,
                collision=attempt,
            )
            destination = directory / candidate
            part = directory / f".{candidate}.{uuid4().hex}.part"
            part_identity: _FileIdentity | None = None
            move: _PublicationMove | None = None
            try:
                part_identity, size, digest = _copy_to_exclusive_part(
                    source,
                    part,
                    max_bytes=self.max_artifact_bytes,
                    expected_sha256=expected_sha256,
                    expected_size=expected_size,
                )
                move = _PublicationMove(
                    destination=destination,
                    part_path=part,
                    sha256=digest,
                    size_bytes=size,
                    part_identity=part_identity,
                    target_kind=target_kind,
                    ordinal=ordinal,
                    target_label=target_label,
                )
                if before_move is not None:
                    before_move(move)
                _move_no_replace(part, destination)
                destination_identity = _identity(destination)
                if destination_identity is None:
                    raise PublicationAuditRequired(
                        f"{target_kind} destination disappeared after no-clobber move"
                    )
                if _hash_file(destination, max_bytes=self.max_artifact_bytes) != digest:
                    raise PublicationAuditRequired(
                        f"{target_kind} destination hash changed after move"
                    )
                move.destination_identity = destination_identity
                move.state = "move_applied"
                if after_move is not None:
                    try:
                        after_move(move)
                    except BaseException as exc:
                        # The native move is known to have happened, but the
                        # applied journal record may or may not have reached
                        # durable storage.  Refuse automatic deletion until
                        # an explicit recovery inspects the durable journal.
                        move.state = "move_applied_unconfirmed"
                        raise PublicationAuditRequired(
                            "move applied journal acknowledgement is unknown"
                        ) from exc
                return move
            except OSError as exc:
                if _is_collision_error(exc):
                    if move is not None:
                        move.state = "collision"
                        if after_move is not None:
                            try:
                                after_move(move)
                            except BaseException as callback_exc:
                                move.state = "collision_unconfirmed"
                                raise PublicationAuditRequired(
                                    "collision journal acknowledgement is unknown"
                                ) from callback_exc
                    _remove_exact_part(part, expected_identity=part_identity)
                    continue
                raise
            except FileExistsError:
                if move is not None:
                    move.state = "collision"
                    if after_move is not None:
                        try:
                            after_move(move)
                        except BaseException as callback_exc:
                            move.state = "collision_unconfirmed"
                            raise PublicationAuditRequired(
                                "collision journal acknowledgement is unknown"
                            ) from callback_exc
                _remove_exact_part(part, expected_identity=part_identity)
                continue
            except BaseException:
                raise
        raise PublicationCollision(f"no collision-free destination for {filename}")

    def _reuse_duplicate_or_copy(
        self,
        item: _PreparedArtifact,
        *,
        attempt_id: str,
        ordinal: int,
        before_move: Callable[[_PublicationMove], None] | None,
        after_move: Callable[[_PublicationMove], None] | None,
    ) -> _PublicationMove:
        duplicate = self._find_stable_duplicate(item.artifact)
        if duplicate is not None:
            destination, identity = duplicate
            move = _PublicationMove(
                destination=destination,
                part_path=Path(""),
                sha256=item.artifact.sha256,
                size_bytes=item.artifact.size_bytes,
                part_identity=identity,
                destination_identity=identity,
                target_kind="canonical",
                ordinal=ordinal,
                owned=False,
                state="reused",
            )
            if before_move is not None:
                before_move(move)
            if after_move is not None:
                after_move(move)
            return move
        return self._copy_with_collision(
            item.source_path,
            item.canonical_directory,
            item.filename,
            expected_sha256=item.artifact.sha256,
            expected_size=item.artifact.size_bytes,
            attempt_id=attempt_id,
            ordinal=ordinal,
            target_kind="canonical",
            before_move=before_move,
            after_move=after_move,
        )

    def _find_stable_duplicate(
        self,
        artifact: StagedArtifact,
    ) -> tuple[Path, _FileIdentity] | None:
        registry_metadata = _validate_registry_file(self.registry_path)
        if registry_metadata is None:
            return None
        try:
            raw = json.loads(self.registry_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PublicationAuditRequired("fingerprint registry cannot be read") from exc
        if not isinstance(raw, dict):
            raise PublicationAuditRequired("fingerprint registry must be an object")
        record = raw.get(artifact.sha256)
        if not isinstance(record, dict):
            return None
        raw_path = record.get("file_path")
        if not isinstance(raw_path, str) or not raw_path:
            return None
        if record.get("fingerprint") != artifact.sha256:
            return None
        path = Path(raw_path)
        try:
            _require_d_directory(path.parent)
            _reject_reparse_components(path, path.anchor and Path(path.anchor) or path.parent)
            resolved = path.resolve(strict=True)
            resolved.relative_to(self.canonical_paths.root.resolve(strict=True))
        except (OSError, ValueError, PublicationError):
            return None
        if not resolved.is_file():
            return None
        allowed = _IMAGE_SUFFIXES if artifact.media_kind == "image" else _VIDEO_SUFFIXES
        if resolved.suffix.lower() not in allowed:
            return None
        metadata = resolved.stat()
        if metadata.st_nlink != 1 or metadata.st_size != artifact.size_bytes:
            return None
        if int(record.get("file_size", -1)) != artifact.size_bytes:
            return None
        before = (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns)
        digest = _hash_file(resolved, max_bytes=self.max_artifact_bytes)
        after_info = resolved.stat()
        after = (
            after_info.st_dev,
            after_info.st_ino,
            after_info.st_size,
            after_info.st_mtime_ns,
        )
        if digest != artifact.sha256 or before != after:
            return None
        identity = _identity(resolved)
        if identity is None:
            return None
        return resolved, identity

    def _publish_registry(self, publication: CanonicalMaterialPublication) -> None:
        registry_path = self.registry_path
        registry_metadata = _validate_registry_file(registry_path)
        old_bytes = registry_path.read_bytes() if registry_metadata is not None else None
        self._old_registry_bytes = old_bytes
        try:
            raw: Any = {} if old_bytes is None else json.loads(old_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PublicationAuditRequired("fingerprint registry is malformed") from exc
        if not isinstance(raw, dict):
            raise PublicationAuditRequired("fingerprint registry must be a JSON object")
        records = dict(raw)
        canonical_moves = [
            move
            for move in publication.moves
            if move.target_kind == "canonical"
            and move.state in {"move_applied", "reused"}
        ]
        for move in canonical_moves:
            if move.ordinal < 0 or move.ordinal >= len(publication.prepared):
                raise PublicationAuditRequired("registry move ordinal is invalid")
            item = publication.prepared[move.ordinal]
            records[item.artifact.sha256] = {
                "fingerprint": item.artifact.sha256,
                "file_path": str(move.destination),
                "source_type": item.artifact.source_type,
                "file_size": item.artifact.size_bytes,
            }
        payload = json.dumps(records, ensure_ascii=False, sort_keys=True).encode("utf-8")
        parent = self._registry_parent_pin
        if parent is None:
            raise PublicationAuditRequired("registry parent is not pinned")
        publication.journal["registry"] = {
            "path": str(registry_path),
            "old_sha256": None if old_bytes is None else hashlib.sha256(old_bytes).hexdigest(),
            "old_bytes_b64": None if old_bytes is None else base64.b64encode(old_bytes).decode("ascii"),
            "old_identity": (
                None
                if registry_metadata is None
                else {
                    "device": int(registry_metadata.st_dev),
                    "inode": int(registry_metadata.st_ino),
                }
            ),
            "new_sha256": hashlib.sha256(payload).hexdigest(),
            "new_identity": None,
            "state": "registry_replace_intent",
        }
        write_durable_json(
            parent=publication.attempt_dir,
            filename="publication-journal.json",
            payload=publication.journal,
            replace=True,
            max_bytes=MAX_PUBLICATION_JOURNAL_BYTES,
        )
        try:
            write_durable_bytes(
                parent=parent,
                filename=registry_path.name,
                payload=payload,
                replace=True,
                max_bytes=MAX_PUBLICATION_JOURNAL_BYTES,
            )
            self._new_registry_sha256 = hashlib.sha256(payload).hexdigest()
            registry_identity = _identity(registry_path)
            publication.journal["registry"]["new_identity"] = (
                None
                if registry_identity is None
                else {
                    "device": registry_identity.device,
                    "inode": registry_identity.inode,
                }
            )
            publication.journal["registry"]["state"] = "registry_replace_applied"
            self._write_journal(publication)
        except BaseException as exc:
            # A replacement may have completed before the acknowledgement or
            # applied journal flush failed.  Preserve an explicit unknown
            # state; rollback must not infer ownership from RAM.
            publication.journal["registry"]["state"] = "registry_replace_unknown"
            try:
                self._write_journal(publication)
            except BaseException:
                pass
            raise PublicationAuditRequired(
                "registry replacement acknowledgement is unknown"
            ) from exc

    def _verify_tentative_ownership(
        self,
        publication: CanonicalMaterialPublication,
    ) -> None:
        for move in publication.moves:
            if move.state not in {"move_applied", "reused"}:
                continue
            current = _identity(move.destination)
            if current != move.destination_identity:
                raise PublicationAuditRequired(
                    "published destination identity changed before completion CAS"
                )
            metadata = move.destination.stat()
            if metadata.st_nlink != 1 or metadata.st_size != move.size_bytes:
                raise PublicationAuditRequired(
                    "published destination metadata changed before completion CAS"
                )
            if _hash_file(move.destination, max_bytes=self.max_artifact_bytes) != move.sha256:
                raise PublicationAuditRequired(
                    "published destination hash changed before completion CAS"
                )
        registry = publication.journal.get("registry") or {}
        expected = registry.get("new_sha256")
        if expected:
            if _validate_registry_file(self.registry_path) is None:
                raise PublicationAuditRequired(
                    "registry disappeared before completion CAS"
                )
            current_bytes = self.registry_path.read_bytes()
            if hashlib.sha256(current_bytes).hexdigest() != expected:
                raise PublicationAuditRequired(
                    "registry identity changed before completion CAS"
                )
            expected_identity = registry.get("new_identity")
            current_identity = _identity(self.registry_path)
            if (
                not isinstance(expected_identity, dict)
                or current_identity is None
                or expected_identity
                != {
                    "device": current_identity.device,
                    "inode": current_identity.inode,
                }
            ):
                raise PublicationAuditRequired(
                    "registry identity changed before completion CAS"
                )

    def _restore_registry(self, publication: CanonicalMaterialPublication) -> None:
        if not publication.journal.get("registry"):
            return
        registry_state = publication.journal["registry"].get("state")
        if registry_state != "registry_replace_applied":
            raise PublicationAuditRequired(
                "registry replacement is not durably attested for rollback"
            )
        parent = self._registry_parent_pin
        if parent is None:
            raise PublicationAuditRequired("registry parent pin is unavailable")
        current_metadata = _validate_registry_file(self.registry_path)
        current = self.registry_path.read_bytes() if current_metadata is not None else None
        expected_new = publication.journal["registry"].get("new_sha256")
        current_identity = _identity(self.registry_path)
        expected_identity = publication.journal["registry"].get("new_identity")
        if (
            current is None
            or hashlib.sha256(current).hexdigest() != expected_new
            or expected_identity is None
            or {
                "device": current_identity.device,
                "inode": current_identity.inode,
            }
            != expected_identity
        ):
            raise PublicationAuditRequired("registry identity changed before rollback")
        old_b64 = publication.journal["registry"].get("old_bytes_b64")
        if old_b64 is None:
            _remove_exact_part(
                self.registry_path,
                expected_identity=current_identity,
                expected_sha256=expected_new,
                expected_size=len(current),
            )
        else:
            old = base64.b64decode(old_b64.encode("ascii"), validate=True)
            write_durable_bytes(
                parent=parent,
                filename=self.registry_path.name,
                payload=old,
                replace=True,
                max_bytes=MAX_PUBLICATION_JOURNAL_BYTES,
            )

    def _canonical_directory(
        self,
        request: MaterialOperationRequest,
        artifact: StagedArtifact,
    ) -> Path:
        if request.operation == "official_capture":
            return self.canonical_paths.official_page_dir
        if request.operation == "pexels":
            return self.canonical_paths.pexels_photo_dir if artifact.media_kind == "image" else self.canonical_paths.pexels_video_dir
        if request.operation == "pixabay":
            return self.canonical_paths.pixabay_photo_dir if artifact.media_kind == "image" else self.canonical_paths.pixabay_video_dir
        if request.operation == "stock_candidates":
            provider = _stock_artifact_provider(artifact)
            if provider == "Pexels":
                return (
                    self.canonical_paths.pexels_photo_dir
                    if artifact.media_kind == "image"
                    else self.canonical_paths.pexels_video_dir
                )
            return (
                self.canonical_paths.pixabay_photo_dir
                if artifact.media_kind == "image"
                else self.canonical_paths.pixabay_video_dir
            )
        if request.operation == "coverr":
            if artifact.media_kind != "video":
                raise PublicationError("coverr publication only accepts video artifacts")
            return self.canonical_paths.coverr_video_dir
        if request.operation == "info_motion":
            if artifact.media_kind != "video":
                raise PublicationError("info_motion publication only accepts video artifacts")
            return self.canonical_paths.generated_motion_dir
        raise PublicationError("operation is not allowed for publication")

    def _project_directory(
        self,
        request: MaterialOperationRequest,
        artifact: StagedArtifact,
        uses: Mapping[str, Any],
    ) -> tuple[tuple[str, Path, Any], ...]:
        if request.operation == "stock_candidates":
            provider = _stock_artifact_provider(artifact)
            target_specs = (
                (
                    ("pexels", ("Pexels素材", "pexels", "pexels_material")),
                    ("all_materials", ("全部素材", "all_materials")),
                )
                if provider == "Pexels"
                else (("all_materials", ("全部素材", "all_materials", "pixabay")),)
            )
        else:
            target_specs = {
            "official_capture": (
                ("official_capture", ("官方录屏", "official_capture", "official_recording")),
            ),
            # Pexels is deliberately published to both the source-specific
            # folder and the project's aggregate material folder.  The old
            # executor already promised this two-target behavior; the
            # publisher must preserve it atomically rather than silently
            # dropping the aggregate copy.
            "pexels": (
                ("pexels", ("Pexels素材", "pexels", "pexels_material")),
                ("all_materials", ("全部素材", "all_materials")),
            ),
            "info_motion": (("info_motion", ("信息动效", "info_motion")),),
            "pixabay": (("all_materials", ("全部素材", "all_materials", "pixabay")),),
            "coverr": (("all_materials", ("全部素材", "all_materials", "coverr")),),
            }[request.operation]

        targets: list[tuple[str, Path, Any]] = []
        for label, aliases in target_specs:
            guard = next((uses[key] for key in aliases if key in uses), None)
            if guard is None:
                raise PublicationError(
                    f"project directory guard is missing for {request.operation}:{label}"
                )
            verify = getattr(guard, "verify", None)
            path_value = getattr(guard, "path", None)
            identities = getattr(guard, "path_identities", None)
            project_use = getattr(guard, "project_use", None)
            if (
                not callable(verify)
                or not isinstance(path_value, Path)
                or not isinstance(identities, tuple)
                or project_use is None
            ):
                raise PublicationError(
                    "project directory must be a prepared child identity guard"
                )
            verify()
            targets.append((label, _require_d_directory(path_value), guard))
        return tuple(targets)

    def _build_result(self, publication: CanonicalMaterialPublication) -> Any:
        from app.pipeline.material_candidate_contract import CandidateEntry
        from app.pipeline.material_execution_service import (
            MaterialCandidateBatchResult,
            MaterialExecutionResult,
        )

        if publication.request.operation == "stock_candidates":
            candidates: list[CandidateEntry] = []
            for index, prepared in enumerate(publication.prepared):
                artifact = prepared.artifact
                provider = _stock_artifact_provider(artifact)
                rights = artifact.rights_evidence
                if not isinstance(rights, Mapping):
                    raise PublicationAuditRequired(
                        "published stock candidate has no structured rights evidence"
                    )
                project_moves = [
                    move
                    for move in publication.moves
                    if move.target_kind == "project"
                    and move.state == "move_applied"
                    and move.ordinal == index
                ]
                selected_move = next(
                    (
                        move
                        for move in project_moves
                        if move.target_label == "all_materials"
                    ),
                    None,
                )
                if selected_move is None:
                    raise PublicationAuditRequired(
                        "stock candidate aggregate project publication move is missing"
                    )
                media_validation = prepared.media_validation
                if media_validation is None:
                    raise PublicationAuditRequired(
                        "stock candidate media validation report is missing"
                    )
                provider_asset_id = str(rights["provider_asset_id"])
                candidate_digest = hashlib.sha256(
                    f"{provider}:{provider_asset_id}".encode("utf-8")
                ).hexdigest()[:20]
                candidates.append(
                    CandidateEntry(
                        candidate_id=f"{provider.casefold()}-{candidate_digest}",
                        source=provider,
                        preview_path=str(selected_move.destination),
                        url=str(rights["asset_page_url"]),
                        rights=str(rights["license_snapshot"]["license_name"]),
                        evidence_kind=artifact.evidence_kind,
                        status="candidate",
                        reason="真实库存素材候选，等待人工确认使用场景与授权风险。",
                        media_validation=copy.deepcopy(media_validation),
                        rights_evidence=copy.deepcopy(dict(rights)),
                    )
                )
            return MaterialCandidateBatchResult(
                success=True,
                status="需要人工确认",
                candidates=tuple(candidates),
                reason="Pexels 与 Pixabay 候选已由父进程原子发布，等待人工确认。",
            )

        if publication.selected_index is None:
            raise PublicationAuditRequired(
                "single-result publication has no selected artifact"
            )
        selected = publication.prepared[publication.selected_index]
        artifact = selected.artifact
        project_moves = [
            move
            for move in publication.moves
            if move.target_kind == "project"
            and move.state == "move_applied"
            and move.ordinal == publication.selected_index
        ]
        if not project_moves:
            raise PublicationAuditRequired(
                "selected project publication move is missing"
            )
        # The aggregate folder is the user-facing path when two project
        # targets exist.  Fall back to the source-specific target for legacy
        # operations that intentionally have only one destination.
        selected_move = next(
            (move for move in project_moves if move.target_label == "all_materials"),
            project_moves[0],
        )
        return MaterialExecutionResult(
            success=True,
            status="已完成",
            material_path=str(selected_move.destination),
            material_type=("图片" if artifact.media_kind == "image" else "视频"),
            source_type=artifact.source_type,
            reason="素材已完成父进程原子发布。",
            evidence_kind=artifact.evidence_kind,
            source_url=artifact.source_url,
            is_locally_generated=artifact.is_locally_generated,
            rights_evidence=copy.deepcopy(artifact.rights_evidence),
        )

    def _build_completion_intent(
        self,
        publication: CanonicalMaterialPublication,
    ) -> dict[str, Any]:
        """Build the immutable parent-result receipt written before reference CAS."""

        result = publication.result
        if result is None or getattr(result, "success", None) is not True:
            raise PublicationAuditRequired(
                "publication completion intent requires a successful parent result"
            )
        published_result = _encode_published_result(result)
        claim_receipt: dict[str, Any] | None = None
        if publication.expected_payload is not None:
            expected = copy.deepcopy(publication.expected_payload)
            revision = expected.get("execution_revision", 0)
            material_task_id = expected.get("active_material_task_id")
            if (
                type(revision) is not int
                or isinstance(revision, bool)
                or revision < 0
                or material_task_id is not None
                and (type(material_task_id) is not str or not material_task_id)
            ):
                raise PublicationAuditRequired(
                    "claim-bound publication has no valid reference-base receipt"
                )
            claim_receipt = {
                "material_task_id": material_task_id,
                "execution_id": publication.request.execution_id,
                "attempt_id": publication.request.attempt_id,
                "operation": publication.request.operation,
                "control_id": expected.get("active_control_id"),
                "job_instance_id": expected.get("active_job_instance_id"),
                "reference_base_revision": revision,
                "reference_base_payload_sha256": _json_sha256(expected),
            }
        intent: dict[str, Any] = {
            "format": "material-publication-completion-intent-v1",
            "claim_receipt": claim_receipt,
            "worker_result_sha256": hashlib.sha256(
                encode_material_worker_result(publication.worker_result)
            ).hexdigest(),
            "published_result": published_result,
            "published_result_sha256": _json_sha256(published_result),
        }
        intent["intent_sha256"] = _json_sha256(intent)
        return intent

    def _payload_with_phase(
        self,
        payload: Mapping[str, Any],
        *,
        phase: str,
        execution_state: str,
        publication: CanonicalMaterialPublication,
    ) -> dict[str, Any]:
        result = copy.deepcopy(dict(payload))
        result["publication_phase"] = phase
        result["execution_state"] = execution_state
        result["active_attempt_id"] = publication.request.attempt_id
        result["execution_id"] = publication.request.execution_id
        result["publication_journal"] = str(
            publication.attempt_dir.path / "publication-journal.json"
        )
        if phase == "db_committed":
            intent = publication.completion_intent
            claim_receipt = intent.get("claim_receipt")
            if not isinstance(claim_receipt, Mapping):
                raise PublicationAuditRequired(
                    "claim-bound reference CAS has no completion-intent claim receipt"
                )
            result.update(
                {
                    "publication_intent_sha256": intent["intent_sha256"],
                    "publication_result_sha256": intent[
                        "published_result_sha256"
                    ],
                    "publication_worker_result_sha256": intent[
                        "worker_result_sha256"
                    ],
                    "publication_reference_base_revision": claim_receipt[
                        "reference_base_revision"
                    ],
                    "publication_reference_base_payload_sha256": claim_receipt[
                        "reference_base_payload_sha256"
                    ],
                }
            )
            _mark_active_material_task_publishing(result)
        revision = result.get("execution_revision")
        if revision is not None:
            if type(revision) is not int or revision < 0:
                raise PublicationAuditRequired(
                    "publication payload has an invalid execution revision"
                )
            result["execution_revision"] = revision + 1
        return result


def _mark_active_material_task_publishing(payload: dict[str, Any]) -> None:
    material_task_id = payload.get("active_material_task_id")
    if material_task_id in (None, ""):
        # Compatibility publishers may not participate in the typed material
        # lifecycle.  Typed claims always carry this immutable task identity.
        return
    if type(material_task_id) is not str:
        raise PublicationAuditRequired("active material task identity is invalid")
    tasks = payload.get("material_tasks")
    if not isinstance(tasks, list):
        raise PublicationAuditRequired("active material task list is missing")
    matches = [
        item
        for item in tasks
        if isinstance(item, dict)
        and item.get("material_task_id") == material_task_id
    ]
    if len(matches) != 1:
        raise PublicationAuditRequired(
            "active material task is not unique during publication"
        )
    task = matches[0]
    history = task.get("status_history")
    if history is None:
        history = []
    if not isinstance(history, list) or any(type(item) is not str for item in history):
        raise PublicationAuditRequired("active material task history is invalid")
    task["status"] = "发布收口中"
    if not history or history[-1] != "发布收口中":
        history.append("发布收口中")
    task["status_history"] = history[-20:]


def _fresh_payload(database: PublicationDatabase) -> dict[str, Any]:
    value = database.fresh_read()
    if not isinstance(value, Mapping):
        raise PublicationAuditRequired("database fresh read is not a mapping")
    return copy.deepcopy(dict(value))


def _selected_index(result: MaterialWorkerResult) -> int | None:
    if result.operation == "stock_candidates":
        if result.selected_artifact != "":
            raise PublicationError(
                "stock candidate publication must not select an artifact"
            )
        return None
    for index, artifact in enumerate(result.artifacts):
        if artifact.relative_path == result.selected_artifact:
            return index
    raise PublicationError("selected artifact is not present in worker result")


def _stock_artifact_provider(artifact: StagedArtifact) -> str:
    rights = artifact.rights_evidence
    if not isinstance(rights, Mapping):
        raise PublicationError(
            "stock candidate publication requires structured rights evidence"
        )
    provider = rights.get("provider")
    if provider not in {"Pexels", "Pixabay"}:
        raise PublicationError("stock candidate rights provider is not supported")
    if artifact.source_type != provider:
        raise PublicationError(
            "stock candidate source_type must match the rights provider"
        )
    media_type = rights.get("media_type")
    if artifact.media_kind == "video":
        matches = media_type == "video"
    else:
        matches = media_type in {"photo", "illustration", "vector"}
    if not matches:
        raise PublicationError(
            "stock candidate media kind must match the rights provider record"
        )
    return provider


def _validate_staged_artifact(
    artifact: StagedArtifact,
    *,
    payload_root: Path,
    max_bytes: int,
) -> Path:
    relative = artifact.relative_path
    if not relative.startswith("payload/"):
        raise ValueError("artifact path must start with payload/")
    parts = PurePosixPath(relative.removeprefix("payload/")).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("artifact path contains traversal")
    source = payload_root.joinpath(*parts)
    _reject_reparse_components(source, payload_root)
    try:
        resolved = source.resolve(strict=True)
        resolved.relative_to(payload_root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise ValueError("artifact path escapes payload") from exc
    if not resolved.is_file():
        raise ValueError("artifact is not a regular file")
    first = resolved.stat()
    if first.st_nlink != 1:
        raise ValueError("artifact is a hard link")
    if first.st_size <= 0 or first.st_size > max_bytes:
        raise ValueError("artifact size is outside the publication limit")
    suffix = resolved.suffix.lower()
    allowed = _IMAGE_SUFFIXES if artifact.media_kind == "image" else _VIDEO_SUFFIXES
    if suffix not in allowed:
        raise ValueError("artifact suffix does not match media kind")
    digest = _hash_file(resolved, max_bytes=max_bytes)
    second = resolved.stat()
    if (
        first.st_size != second.st_size
        or first.st_dev != second.st_dev
        or first.st_mtime_ns != second.st_mtime_ns
        or first.st_ino != second.st_ino
        or first.st_nlink != second.st_nlink
        or digest != artifact.sha256
        or first.st_size != artifact.size_bytes
    ):
        raise ValueError("artifact changed while being validated")
    validate_execution_provenance(
        success=True,
        evidence_kind=artifact.evidence_kind,
        source_url=artifact.source_url,
        is_locally_generated=artifact.is_locally_generated,
    )
    return resolved


def _validate_declared_payload_files(
    payload_root: Path,
    declared_paths: tuple[Path, ...],
) -> None:
    """Reject any regular payload file not named by the worker result.

    Empty implementation directories are allowed because the worker creates a
    fixed path tree before invoking a provider.  Any non-declared bytes,
    reparse point, or hard link is instead treated as a stale/extra artifact.
    """

    root = payload_root.resolve(strict=True)
    declared = {path.resolve(strict=True) for path in declared_paths}
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        for name in directories:
            candidate = current_path / name
            metadata = candidate.lstat()
            if candidate.is_symlink() or int(getattr(metadata, "st_file_attributes", 0)) & 0x0400:
                raise PublicationError("payload tree contains a reparse directory")
        for name in files:
            candidate = current_path / name
            metadata = candidate.lstat()
            if candidate.is_symlink() or int(getattr(metadata, "st_file_attributes", 0)) & 0x0400:
                raise PublicationError("payload tree contains a reparse file")
            if metadata.st_nlink != 1:
                raise PublicationError("payload tree contains an undeclared hard link")
            resolved = candidate.resolve(strict=True)
            if resolved not in declared:
                raise PublicationError(
                    f"payload contains an undeclared file: {candidate.name}"
                )


def _reject_reparse_components(path: Path, payload_root: Path) -> None:
    current = payload_root
    relative = path.relative_to(payload_root)
    for part in relative.parts:
        current = current / part
        metadata = current.lstat()
        attributes = int(getattr(metadata, "st_file_attributes", 0))
        if current.is_symlink() or attributes & 0x0400:
            raise ValueError("artifact path contains a reparse point")


def _require_d_directory(path: Path) -> Path:
    lexical = Path(path)
    _reject_reparse_ancestors(lexical)
    try:
        resolved = lexical.resolve(strict=True)
    except OSError as exc:
        raise PublicationError("publication directory cannot be resolved") from exc
    if ntpath.normcase(str(lexical)) != ntpath.normcase(str(resolved)):
        raise PublicationError("publication directory lexical path changed physically")
    if str(resolved).replace("/", "\\")[:2].casefold() != "d:" or not resolved.is_dir():
        raise PublicationError("publication paths must be existing D directories")
    metadata = resolved.stat()
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    if attributes & 0x0400 or resolved.is_symlink():
        raise PublicationError("publication directory cannot be a reparse point")
    return resolved


def _reject_reparse_ancestors(path: Path) -> None:
    """Inspect lexical components before ``Path.resolve`` can follow them."""

    if not path.anchor:
        return
    current = Path(path.anchor)
    for part in path.parts[1:]:
        if part in {".", ".."}:
            raise PublicationError("publication directory contains traversal")
        current = current / part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            # The final existence check will produce the domain error; do not
            # silently treat a missing ancestor as a safe path.
            continue
        except OSError as exc:
            raise PublicationError("publication directory cannot be inspected") from exc
        if current.is_symlink() or int(getattr(metadata, "st_file_attributes", 0)) & 0x0400:
            raise PublicationError("publication directory contains a reparse point")


def _validate_registry_file(path: Path) -> os.stat_result | None:
    """Return a safe registry snapshot target, or ``None`` when absent.

    A registry is a control-plane file, so following a symlink/junction or
    replacing a hard link would make the durable journal point at bytes outside
    the approved canonical tree.  Reject those states before *any* read or
    replace operation.
    """

    _require_d_directory(path.parent)
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise PublicationAuditRequired("fingerprint registry cannot be inspected") from exc
    if path.is_symlink() or int(getattr(metadata, "st_file_attributes", 0)) & 0x0400:
        raise PublicationAuditRequired("fingerprint registry is a reparse point")
    if not path.is_file():
        raise PublicationAuditRequired("fingerprint registry is not a regular file")
    if int(metadata.st_nlink) != 1:
        raise PublicationAuditRequired("fingerprint registry is a hard link")
    if int(metadata.st_size) > MAX_PUBLICATION_JOURNAL_BYTES:
        raise PublicationAuditRequired("fingerprint registry exceeds the size limit")
    return metadata


def _validate_canonical_directories(paths: AutoMaterialPaths) -> None:
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
    )
    for directory in directories:
        _require_d_directory(directory)
        pin = approve_physical_d_write_root(directory)
        pin.close()


def _hash_file(path: Path, *, max_bytes: int) -> str:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            total += len(chunk)
            if total > max_bytes:
                raise ValueError("artifact exceeded publication limit while hashing")
            digest.update(chunk)
    return digest.hexdigest()


def _identity(path: Path) -> _FileIdentity | None:
    try:
        info = path.stat()
    except OSError:
        return None
    return _FileIdentity(device=int(info.st_dev), inode=int(info.st_ino))


def _copy_to_exclusive_part(
    source: Path,
    part: Path,
    *,
    max_bytes: int,
    expected_sha256: str | None,
    expected_size: int | None,
) -> tuple[_FileIdentity, int, str]:
    source_stat = source.stat()
    if source_stat.st_size <= 0 or source_stat.st_size > max_bytes:
        raise ValueError("source size is outside publication limit")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    fd = os.open(str(part), flags, 0o600)
    created_info = os.fstat(fd)
    created_identity = _FileIdentity(
        device=int(created_info.st_dev),
        inode=int(created_info.st_ino),
    )
    created_attributes = int(getattr(created_info, "st_file_attributes", 0))
    if created_attributes & (0x0010 | 0x0400) or int(created_info.st_nlink) != 1:
        os.close(fd)
        raise PublicationAuditRequired("exclusive part identity is not a plain file")

    pending_error: BaseException | None = None
    digest = hashlib.sha256()
    digest_value = ""
    size = 0
    try:
        try:
            os.set_handle_inheritable(fd, False)
        except (AttributeError, OSError):
            pass
        with source.open("rb") as source_stream, os.fdopen(fd, "wb", closefd=True) as target:
            fd = -1
            while True:
                chunk = source_stream.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError("source exceeded publication limit")
                digest.update(chunk)
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
        source_after = source.stat()
        digest_value = digest.hexdigest()
        if (
            size != source_stat.st_size
            or source_after.st_dev != source_stat.st_dev
            or source_after.st_ino != source_stat.st_ino
            or source_after.st_mtime_ns != source_stat.st_mtime_ns
            or source_after.st_nlink != 1
            or (expected_size is not None and size != expected_size)
            or (expected_sha256 is not None and digest_value != expected_sha256)
        ):
            raise PublicationAuditRequired("source changed during publication copy")
    except BaseException as exc:
        pending_error = exc
    finally:
        if fd != -1:
            try:
                os.close(fd)
            except OSError as exc:
                pending_error = PublicationAuditRequired(
                    "exclusive part close acknowledgement is unknown"
                )

    if pending_error is not None:
        try:
            _remove_exact_part(part, expected_identity=created_identity)
        except BaseException as cleanup_exc:
            raise PublicationAuditRequired(
                "failed copy left an exclusive part that cannot be safely removed"
            ) from cleanup_exc
        raise pending_error

    identity = _identity(part)
    if identity != created_identity:
        raise PublicationAuditRequired("exclusive part identity changed before move")
    return identity, size, digest_value


def _move_no_replace(source: Path, destination: Path) -> None:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.MoveFileExW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
        kernel32.MoveFileExW.restype = wintypes.BOOL
        # MOVEFILE_WRITE_THROUGH, deliberately without REPLACE_EXISTING.
        if not kernel32.MoveFileExW(str(source), str(destination), 0x00000008):
            error = ctypes.get_last_error()
            raise OSError(error, "MoveFileExW no-replace failed", str(destination))
        return
    # This product contract is Windows/D: only.  POSIX ``rename`` may replace
    # an existing destination, so silently using it would turn a development
    # run into a false no-clobber claim.
    raise PublicationError("no-replace publication requires Windows")


def _is_collision_error(error: OSError) -> bool:
    return (
        isinstance(error, FileExistsError)
        or getattr(error, "winerror", None) in {80, 183}
        or getattr(error, "errno", None) in {80, 183}
    )


def _remove_exact_part(
    path: Path,
    *,
    expected_identity: _FileIdentity | None,
    expected_sha256: str | None = None,
    expected_size: int | None = None,
) -> None:
    """Delete only the exact file proven by an open, DELETE-capable handle.

    A path ``stat`` followed by ``Path.unlink`` has a replacement race: an
    attacker (or another publisher) can swap the directory entry between the
    two calls.  Windows gives us the stronger primitive required by the
    publication contract: open the target with ``FILE_FLAG_OPEN_REPARSE_POINT``,
    re-check identity/hash through that handle, and set
    ``FileDispositionInfo`` on the same handle.  Non-Windows execution is
    deliberately rejected instead of pretending that POSIX unlink has the
    same proof.
    """

    if os.name != "nt":
        raise PublicationAuditRequired("exact handle-bound delete requires Windows")
    if expected_identity is None:
        raise PublicationAuditRequired("exact cleanup requires an expected file identity")

    # The exact-file proof is only meaningful inside the approved physical D
    # tree.  This also rejects a swapped/reparse parent before CreateFileW.
    _require_d_directory(path.parent)

    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.SetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        wintypes.INT,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    kernel32.SetFileInformationByHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    # GENERIC_READ | DELETE | FILE_READ_ATTRIBUTES.  No sharing is allowed
    # while the ownership proof and disposition are in flight: otherwise a
    # second handle could rewrite the bytes after the hash check.
    desired_access = 0x80000000 | 0x00010000 | 0x00000080
    share_mode = 0
    open_existing = 3
    open_reparse_point = 0x00200000
    handle = kernel32.CreateFileW(
        str(path),
        desired_access,
        share_mode,
        None,
        open_existing,
        open_reparse_point,
        None,
    )
    invalid_handle = wintypes.HANDLE(-1).value
    if handle == invalid_handle:
        error = ctypes.get_last_error()
        if error in {2, 3}:
            return
        raise PublicationAuditRequired(
            f"exact file handle could not be opened (winerror={error})"
        )

    fd = -1
    delete_requested = False
    try:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        fd = msvcrt.open_osfhandle(int(handle), flags)
        handle = wintypes.HANDLE(0)
        info = os.fstat(fd)
        attributes = int(getattr(info, "st_file_attributes", 0))
        if attributes & 0x0400:
            raise PublicationAuditRequired("exact file is a reparse point")
        if attributes & 0x0010:
            raise PublicationAuditRequired("exact cleanup target is a directory")
        current = _FileIdentity(device=int(info.st_dev), inode=int(info.st_ino))
        if current != expected_identity:
            raise PublicationAuditRequired("temporary part identity changed")
        if int(info.st_nlink) != 1:
            raise PublicationAuditRequired("temporary part is not a single-link file")
        if expected_size is not None and int(info.st_size) != expected_size:
            raise PublicationAuditRequired("temporary part size changed")
        before_delete = (
            int(info.st_dev),
            int(info.st_ino),
            int(info.st_size),
            int(info.st_mtime_ns),
            int(info.st_nlink),
        )
        if expected_sha256 is not None:
            def hash_open_file() -> str:
                digest = hashlib.sha256()
                total = 0
                os.lseek(fd, 0, os.SEEK_SET)
                with os.fdopen(os.dup(fd), "rb", closefd=True) as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        total += len(chunk)
                        if total > max(expected_size or int(info.st_size), 1):
                            raise PublicationAuditRequired(
                                "temporary part exceeds expected size"
                            )
                        digest.update(chunk)
                return digest.hexdigest()

            digest_before = hash_open_file()
            after_info = os.fstat(fd)
            after_delete = (
                int(after_info.st_dev),
                int(after_info.st_ino),
                int(after_info.st_size),
                int(after_info.st_mtime_ns),
                int(after_info.st_nlink),
            )
            digest_after = hash_open_file()
            if before_delete != after_delete or digest_before != digest_after:
                raise PublicationAuditRequired("temporary part changed during exact cleanup")
            if digest_after != expected_sha256:
                raise PublicationAuditRequired("temporary part hash changed")

        class _FileDispositionInfo(ctypes.Structure):
            _fields_ = [("DeleteFile", wintypes.BOOLEAN)]

        disposition = _FileDispositionInfo(1)
        native_handle = msvcrt.get_osfhandle(fd)
        if not kernel32.SetFileInformationByHandle(
            wintypes.HANDLE(native_handle),
            4,  # FileDispositionInfo
            ctypes.byref(disposition),
            ctypes.sizeof(disposition),
        ):
            error = ctypes.get_last_error()
            raise PublicationAuditRequired(
                f"handle-bound delete could not be requested (winerror={error})"
            )
        delete_requested = True
    finally:
        if fd != -1:
            try:
                os.close(fd)
            except OSError as exc:
                if delete_requested:
                    raise PublicationAuditRequired(
                        "handle-bound delete close acknowledgement is unknown"
                    ) from exc
        elif handle not in {None, wintypes.HANDLE(0).value, invalid_handle}:
            if not kernel32.CloseHandle(handle):
                error = ctypes.get_last_error()
                raise PublicationAuditRequired(
                    f"exact file handle close acknowledgement is unknown (winerror={error})"
                )


def _validate_referenced_publishing_payload(
    payload: Mapping[str, Any],
    request: MaterialOperationRequest,
    attempt_path: Path,
) -> None:
    base_revision = payload.get("publication_reference_base_revision")
    revision = payload.get("execution_revision")
    journal_path = payload.get("publication_journal")
    expected_journal = str(attempt_path / "publication-journal.json")
    receipt_fields = (
        payload.get("publication_intent_sha256"),
        payload.get("publication_result_sha256"),
        payload.get("publication_worker_result_sha256"),
        payload.get("publication_reference_base_payload_sha256"),
    )
    if (
        payload.get("publication_phase") != "db_committed"
        or payload.get("execution_state") != "publishing"
        or payload.get("execution_id") != request.execution_id
        or payload.get("active_execution_id") != request.execution_id
        or payload.get("active_attempt_id") != request.attempt_id
        or payload.get("active_attempt_kind") != request.operation
        or type(payload.get("active_material_task_id")) is not str
        or not payload.get("active_material_task_id")
        or type(payload.get("active_control_id")) is not str
        or not payload.get("active_control_id")
        or type(payload.get("active_job_instance_id")) is not str
        or not payload.get("active_job_instance_id")
        or type(base_revision) is not int
        or isinstance(base_revision, bool)
        or type(revision) is not int
        or isinstance(revision, bool)
        or revision != base_revision + 1
        or type(journal_path) is not str
        or ntpath.normcase(journal_path) != ntpath.normcase(expected_journal)
        or any(
            type(value) is not str
            or re.fullmatch(r"[0-9a-f]{64}", value) is None
            for value in receipt_fields
        )
    ):
        raise PublicationAuditRequired(
            "database does not reference this exact recoverable publication"
        )


def _validate_referenced_completed_payload(
    payload: Mapping[str, Any],
    request: MaterialOperationRequest,
    attempt_path: Path,
    *,
    checkpoint: MaterialRunnerRetirementCheckpoint,
) -> None:
    try:
        current_checkpoint = pending_runner_retirement_from_payload(payload)
    except ValueError as exc:
        raise PublicationAuditRequired(
            "database does not contain an exact pending completion"
        ) from exc
    expected_journal = str(attempt_path / "publication-journal.json")
    journal_path = payload.get("publication_journal")
    if (
        current_checkpoint != checkpoint
        or checkpoint.completion_status != "succeeded"
        or checkpoint.execution_id != request.execution_id
        or checkpoint.attempt_id != request.attempt_id
        or checkpoint.operation != request.operation
        or type(journal_path) is not str
        or ntpath.normcase(journal_path) != ntpath.normcase(expected_journal)
    ):
        raise PublicationAuditRequired(
            "database pending completion does not reference this publication"
        )


def _read_recovery_publication_journal(path: Path) -> tuple[dict[str, Any], str]:
    try:
        before = path.lstat()
        attributes = int(getattr(before, "st_file_attributes", 0))
        if path.is_symlink() or attributes & 0x0400:
            raise PublicationAuditRequired("publication journal is a reparse point")
        if not path.is_file() or before.st_nlink != 1:
            raise PublicationAuditRequired(
                "publication journal is not an owned regular file"
            )
        if before.st_size <= 0 or before.st_size > MAX_PUBLICATION_JOURNAL_BYTES:
            raise PublicationAuditRequired("publication journal size is invalid")
        raw = path.read_bytes()
        after = path.lstat()
    except PublicationAuditRequired:
        raise
    except OSError as exc:
        raise PublicationAuditRequired(
            "publication journal cannot be read for recovery"
        ) from exc
    before_identity = (
        int(before.st_dev),
        int(before.st_ino),
        int(before.st_size),
        int(before.st_mtime_ns),
        int(before.st_nlink),
    )
    after_identity = (
        int(after.st_dev),
        int(after.st_ino),
        int(after.st_size),
        int(after.st_mtime_ns),
        int(after.st_nlink),
    )
    if before_identity != after_identity or len(raw) != before.st_size:
        raise PublicationAuditRequired("publication journal changed while being read")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PublicationAuditRequired(
            "publication journal is not valid UTF-8 JSON"
        ) from exc
    base_fields = {
        "format",
        "execution_id",
        "attempt_id",
        "operation",
        "phase",
        "moves",
        "registry",
        "completion_intent",
    }
    if type(value) is not dict or frozenset(value) not in {
        frozenset(base_fields),
        frozenset({*base_fields, "attestation"}),
    }:
        raise PublicationAuditRequired(
            "publication journal has an unexpected recovery schema"
        )
    return copy.deepcopy(value), hashlib.sha256(raw).hexdigest()


def _recovery_file_identity(value: Any, *, label: str) -> _FileIdentity:
    if (
        type(value) is not dict
        or set(value) != {"device", "inode"}
        or any(
            type(value[key]) is not int
            or isinstance(value[key], bool)
            or value[key] < 0
            for key in ("device", "inode")
        )
    ):
        raise PublicationAuditRequired(f"{label} identity is invalid")
    return _FileIdentity(device=value["device"], inode=value["inode"])


def _recovery_directory_key(path: Path) -> str:
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise PublicationAuditRequired(
            "publication target directory cannot be resolved"
        ) from exc
    return ntpath.normcase(str(resolved))


def _recover_publication_moves(
    publication: CanonicalMaterialPublication,
) -> list[_PublicationMove]:
    journal = publication.journal
    if (
        journal.get("format") != "material-publication-v1"
        or journal.get("execution_id") != publication.request.execution_id
        or journal.get("attempt_id") != publication.request.attempt_id
        or journal.get("operation") != publication.request.operation
        or type(journal.get("moves")) is not list
        or not journal.get("moves")
        or type(journal.get("registry")) is not dict
    ):
        raise PublicationAuditRequired(
            "publication journal does not belong to this recovery attempt"
        )
    expected_targets: dict[tuple[str, str, int], str] = {}
    for ordinal, prepared in enumerate(publication.prepared):
        expected_targets[("canonical", "", ordinal)] = _recovery_directory_key(
            prepared.canonical_directory
        )
        for label, directory, guard in prepared.project_targets:
            verify = getattr(guard, "verify", None)
            if callable(verify):
                verify()
            expected_targets[("project", label, ordinal)] = _recovery_directory_key(
                directory
            )

    fields = {
        "destination",
        "part_path",
        "sha256",
        "size_bytes",
        "target_kind",
        "ordinal",
        "target_label",
        "owned",
        "part_identity",
        "destination_identity",
        "state",
    }
    recovered: list[_PublicationMove] = []
    seen: set[tuple[str, str, int]] = set()
    for raw in journal["moves"]:
        if type(raw) is not dict or set(raw) != fields:
            raise PublicationAuditRequired(
                "publication move has an unexpected recovery schema"
            )
        key = (raw["target_kind"], raw["target_label"], raw["ordinal"])
        if key in seen or key not in expected_targets:
            raise PublicationAuditRequired(
                "publication move target is duplicated or not expected"
            )
        seen.add(key)
        ordinal = raw["ordinal"]
        if type(ordinal) is not int or isinstance(ordinal, bool):
            raise PublicationAuditRequired("publication move ordinal is invalid")
        prepared = publication.prepared[ordinal]
        destination = Path(raw["destination"])
        state = raw["state"]
        if (
            type(raw["destination"]) is not str
            or not ntpath.isabs(raw["destination"])
            or ntpath.splitdrive(raw["destination"])[0].casefold() != "d:"
            or _recovery_directory_key(destination.parent) != expected_targets[key]
            or raw["sha256"] != prepared.artifact.sha256
            or raw["size_bytes"] != prepared.artifact.size_bytes
            or state not in {"move_applied", "reused"}
            or raw["owned"] is not (state == "move_applied")
            or raw["target_kind"] == "project" and state != "move_applied"
        ):
            raise PublicationAuditRequired(
                "publication move does not match the expected artifact target"
            )
        part_path = Path(raw["part_path"])
        if state == "move_applied":
            if (
                type(raw["part_path"]) is not str
                or not ntpath.isabs(raw["part_path"])
                or _recovery_directory_key(part_path.parent) != expected_targets[key]
                or part_path.exists()
            ):
                raise PublicationAuditRequired(
                    "publication part path is not durably closed"
                )
        elif str(part_path) not in {".", "", str(destination)}:
            raise PublicationAuditRequired(
                "reused publication has an unexpected part path"
            )
        recovered.append(
            _PublicationMove(
                destination=destination,
                part_path=part_path,
                sha256=raw["sha256"],
                size_bytes=raw["size_bytes"],
                part_identity=_recovery_file_identity(
                    raw["part_identity"], label="publication part"
                ),
                target_kind=raw["target_kind"],
                ordinal=ordinal,
                target_label=raw["target_label"],
                owned=raw["owned"],
                destination_identity=_recovery_file_identity(
                    raw["destination_identity"], label="publication destination"
                ),
                state=state,
            )
        )
    if seen != set(expected_targets):
        raise PublicationAuditRequired(
            "publication journal does not contain every expected target"
        )
    registry = journal["registry"]
    registry_fields = {
        "path",
        "old_sha256",
        "old_bytes_b64",
        "old_identity",
        "new_sha256",
        "new_identity",
        "state",
    }
    if (
        set(registry) != registry_fields
        or ntpath.normcase(str(registry.get("path")))
        != ntpath.normcase(str(publication.publisher.registry_path))
        or registry.get("state") != "registry_replace_applied"
        or type(registry.get("new_sha256")) is not str
        or re.fullmatch(r"[0-9a-f]{64}", registry["new_sha256"]) is None
    ):
        raise PublicationAuditRequired(
            "publication registry receipt is not recoverable"
        )
    expected_registry_identity = _recovery_file_identity(
        registry.get("new_identity"), label="publication registry"
    )
    if _identity(publication.publisher.registry_path) != expected_registry_identity:
        raise PublicationAuditRequired(
            "publication registry identity changed before recovery"
        )
    registry_bytes = publication.publisher.registry_path.read_bytes()
    if hashlib.sha256(registry_bytes).hexdigest() != registry["new_sha256"]:
        raise PublicationAuditRequired(
            "publication registry hash changed before recovery"
        )
    return recovered


def _validate_recovery_completion_intent(
    publication: CanonicalMaterialPublication,
    *,
    expected_payload: Mapping[str, Any],
    completed_checkpoint: MaterialRunnerRetirementCheckpoint | None = None,
) -> None:
    intent = publication.journal.get("completion_intent")
    if type(intent) is not dict or set(intent) != {
        "format",
        "claim_receipt",
        "worker_result_sha256",
        "published_result",
        "published_result_sha256",
        "intent_sha256",
    }:
        raise PublicationAuditRequired(
            "publication completion intent has an unexpected schema"
        )
    claim = intent["claim_receipt"]
    if type(claim) is not dict or set(claim) != {
        "material_task_id",
        "execution_id",
        "attempt_id",
        "operation",
        "control_id",
        "job_instance_id",
        "reference_base_revision",
        "reference_base_payload_sha256",
    }:
        raise PublicationAuditRequired(
            "publication completion intent has no claim receipt"
        )
    intent_without_hash = copy.deepcopy(intent)
    intent_sha256 = intent_without_hash.pop("intent_sha256")
    published_result = intent["published_result"]
    if type(published_result) is not dict:
        raise PublicationAuditRequired(
            "publication intent result is not a JSON object"
        )
    worker_sha256 = hashlib.sha256(
        encode_material_worker_result(publication.worker_result)
    ).hexdigest()
    request = publication.request
    if completed_checkpoint is None:
        expected_material_task_id = expected_payload.get("active_material_task_id")
        expected_control_id = expected_payload.get("active_control_id")
        expected_job_instance_id = expected_payload.get("active_job_instance_id")
        expected_base_revision = expected_payload.get(
            "publication_reference_base_revision"
        )
        expected_base_payload_sha256 = expected_payload.get(
            "publication_reference_base_payload_sha256"
        )
    else:
        expected_material_task_id = completed_checkpoint.material_task_id
        expected_control_id = completed_checkpoint.control_id
        expected_job_instance_id = completed_checkpoint.job_instance_id
        expected_base_revision = (
            completed_checkpoint.publication_reference_base_revision
        )
        expected_base_payload_sha256 = (
            completed_checkpoint.publication_reference_base_payload_sha256
        )
    if (
        intent["format"] != "material-publication-completion-intent-v1"
        or intent_sha256 != _json_sha256(intent_without_hash)
        or intent["published_result_sha256"] != _json_sha256(published_result)
        or intent["worker_result_sha256"] != worker_sha256
        or _encode_published_result(publication.result) != published_result
        or claim["material_task_id"] != expected_material_task_id
        or claim["execution_id"] != request.execution_id
        or claim["attempt_id"] != request.attempt_id
        or claim["operation"] != request.operation
        or claim["control_id"] != expected_control_id
        or claim["job_instance_id"] != expected_job_instance_id
        or claim["reference_base_revision"] != expected_base_revision
        or claim["reference_base_payload_sha256"]
        != expected_base_payload_sha256
        or intent_sha256 != expected_payload.get("publication_intent_sha256")
        or intent["published_result_sha256"]
        != expected_payload.get("publication_result_sha256")
        or worker_sha256
        != expected_payload.get("publication_worker_result_sha256")
    ):
        raise PublicationAuditRequired(
            "publication completion intent does not match the referenced database owner"
        )


def _json_sha256(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        dict(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _encode_published_result(value: Any) -> dict[str, Any]:
    from app.pipeline.material_execution_service import (
        MaterialCandidateBatchResult,
        MaterialExecutionResult,
    )

    if type(value) is MaterialCandidateBatchResult:
        return {
            "kind": "stock_candidate_batch",
            "success": value.success,
            "status": value.status,
            "reason": value.reason,
            "candidates": [candidate.to_dict() for candidate in value.candidates],
        }
    if type(value) is MaterialExecutionResult:
        return {
            "kind": "material_execution_result",
            "success": value.success,
            "status": value.status,
            "material_path": value.material_path,
            "material_type": value.material_type,
            "source_type": value.source_type,
            "reason": value.reason,
            "evidence_kind": value.evidence_kind,
            "source_url": value.source_url,
            "is_locally_generated": value.is_locally_generated,
            "rights_evidence": copy.deepcopy(value.rights_evidence),
        }
    raise PublicationAuditRequired(
        "publication result has no supported durable representation"
    )


def _safe_filename(value: str) -> str:
    name = _SAFE_NAME_RE.sub("_", str(value)).strip(" ._")
    if not name:
        name = f"material-{uuid4().hex}"
    name = name[:120]
    try:
        _storage_policy._validate_child_name(name)
    except Exception as exc:
        raise PublicationError("artifact filename is not a safe Windows basename") from exc
    return name


def _collision_name(
    filename: str,
    *,
    attempt_id: str,
    ordinal: int,
    collision: int,
) -> str:
    if collision == 0:
        return filename
    stem = Path(filename).stem[:80]
    suffix = Path(filename).suffix.lower()
    return f"{stem}-{attempt_id[:8]}-{ordinal}-{collision}{suffix}"[:120]


__all__ = [
    "CanonicalMaterialArtifactPublisher",
    "CanonicalMaterialPublication",
    "MAX_PUBLICATION_ARTIFACT_BYTES",
    "PublicationAuditRequired",
    "PublicationCollision",
    "PublicationDatabase",
    "PublicationError",
    "PublicationRejected",
]
