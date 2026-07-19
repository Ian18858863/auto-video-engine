from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from app.core.runtime_identity import require_non_system_drive
from sqlalchemy.orm import Session


MAX_PROJECT_KEY_LENGTH = 80
MAX_PROJECT_SLUG_LENGTH = 48
PROJECT_DIRECTORY_MANIFEST_FORMAT = "project-directory-plan-v1"
PROJECT_REFERENCE_FORMAT = "project-directory-reference-v1"
PROJECT_REFERENCE_FILENAME = ".project-identity.json"
_PROJECT_KEY_PATTERN = re.compile(r"^[a-z0-9-]+$")
_PROJECT_SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

INSERT_GUARD_TRIGGER = "trg_production_pipelines_project_identity_insert_guard"
CANONICALIZE_TRIGGER = "trg_production_pipelines_project_identity_canonicalize"
IMMUTABLE_TRIGGER = "trg_production_pipelines_project_identity_immutable"


def _pending_predicate(alias: str) -> str:
    slug = f"substr({alias}.project_key, 42)"
    return (
        f"length({alias}.project_key) BETWEEN 42 AND 89 "
        f"AND substr({alias}.project_key, 1, 8) = 'pending-' "
        f"AND length(substr({alias}.project_key, 9, 32)) = 32 "
        f"AND substr({alias}.project_key, 9, 32) "
        "NOT GLOB '*[^0-9a-f]*' "
        f"AND substr({alias}.project_key, 41, 1) = '-' "
        f"AND length({slug}) BETWEEN 1 AND 48 "
        f"AND {slug} NOT GLOB '*[^a-z0-9-]*' "
        f"AND substr({slug}, 1, 1) != '-' "
        f"AND substr({slug}, -1, 1) != '-' "
        f"AND {slug} = project_slug_v1({alias}.project_name)"
    )


SQLITE_PROJECT_IDENTITY_TRIGGER_SQL = {
    INSERT_GUARD_TRIGGER: f"""CREATE TRIGGER {INSERT_GUARD_TRIGGER}
BEFORE INSERT ON production_pipelines
FOR EACH ROW
WHEN EXISTS (
    SELECT 1 FROM production_pipelines AS existing WHERE existing.id = NEW.id
)
OR NOT ({_pending_predicate("NEW")})
BEGIN
    SELECT CASE
        WHEN EXISTS (
            SELECT 1 FROM production_pipelines AS existing WHERE existing.id = NEW.id
        )
        THEN RAISE(ABORT, 'project identity replacement is forbidden')
    END;
    SELECT RAISE(ABORT, 'invalid pending project identity');
END""",
    CANONICALIZE_TRIGGER: f"""CREATE TRIGGER {CANONICALIZE_TRIGGER}
AFTER INSERT ON production_pipelines
FOR EACH ROW
BEGIN
    SELECT CASE
        WHEN NEW.id < 1 OR NEW.id > 9223372036854775807
        THEN RAISE(ABORT, 'invalid project identity id')
    END;
    UPDATE production_pipelines
    SET project_key = 'p-' || NEW.id || '-' || project_slug_v1(NEW.project_name)
    WHERE id = NEW.id;
END""",
    IMMUTABLE_TRIGGER: f"""CREATE TRIGGER {IMMUTABLE_TRIGGER}
BEFORE UPDATE OF id, project_key, legacy_project_dir ON production_pipelines
FOR EACH ROW
WHEN (
    NEW.id IS NOT OLD.id
    OR NEW.project_key IS NOT OLD.project_key
    OR NEW.legacy_project_dir IS NOT OLD.legacy_project_dir
)
AND NOT (
    NEW.id = OLD.id
    AND NEW.legacy_project_dir IS OLD.legacy_project_dir
    AND ({_pending_predicate("OLD")})
    AND NEW.project_key = 'p-' || OLD.id || '-' || project_slug_v1(OLD.project_name)
)
BEGIN
    SELECT RAISE(ABORT, 'project identity is immutable');
END""",
}


class ProjectIdentityError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PreparedPipelineIdentity:
    """Small immutable project identity detached from an ORM Session."""

    id: int
    project_name: str
    project_key: str
    project_dir: str
    legacy_project_dir: str | None = None

    def __post_init__(self) -> None:
        pipeline_id = _require_pipeline_id(self.id)
        if not isinstance(self.project_name, str):
            raise ProjectIdentityError("project_name must be a string")
        if not isinstance(self.project_key, str) or not _valid_project_key_for_id(
            self.project_key,
            pipeline_id,
        ):
            raise ProjectIdentityError(
                f"project_key mismatch for pipeline {pipeline_id}: {self.project_key!r}"
            )
        if not isinstance(self.project_dir, str) or not self.project_dir:
            raise ProjectIdentityError("prepared project_dir is required")
        if self.legacy_project_dir is not None and not isinstance(
            self.legacy_project_dir,
            str,
        ):
            raise ProjectIdentityError("legacy_project_dir must be a string or null")

    @classmethod
    def from_pipeline(cls, pipeline: Any) -> "PreparedPipelineIdentity":
        """Freeze only fields used by project-directory verification."""

        return cls(
            id=getattr(pipeline, "id", None),
            project_name=getattr(pipeline, "project_name", None),
            project_key=getattr(pipeline, "project_key", None),
            project_dir=getattr(pipeline, "project_dir", None),
            legacy_project_dir=getattr(pipeline, "legacy_project_dir", None),
        )


@dataclass(frozen=True, slots=True)
class PreparedProjectDirectoryUse:
    pipeline: Any
    project_root: Path
    system_drive: str
    path: Path
    path_identity: tuple[int, int, int]

    def verify(self) -> None:
        _require_path_identity(
            self.path,
            self.path_identity,
            label="prepared project target",
        )
        resolved = resolve_prepared_project_directory(
            self.pipeline,
            project_root=self.project_root,
            system_drive=self.system_drive,
        )
        if resolved != self.path:
            raise ProjectIdentityError(
                "prepared project target changed during filesystem use"
            )


@dataclass(frozen=True, slots=True)
class PreparedProjectChildDirectoryUse:
    project_use: PreparedProjectDirectoryUse
    path: Path
    path_identities: tuple[tuple[Path, tuple[int, int, int]], ...]

    def verify(self) -> None:
        self.project_use.verify()
        for path, expected in self.path_identities:
            _require_path_identity(path, expected, label="prepared project child")
            _require_plain_directory(path, label="prepared project child")
        self.project_use.verify()


def begin_prepared_project_directory_use(
    pipeline: Any,
    *,
    project_root: Path,
    system_drive: str | None = None,
) -> PreparedProjectDirectoryUse:
    effective_system_drive = _effective_system_drive(system_drive)
    root = Path(project_root)
    target = resolve_prepared_project_directory(
        pipeline,
        project_root=root,
        system_drive=effective_system_drive,
    )
    return PreparedProjectDirectoryUse(
        pipeline=pipeline,
        project_root=root,
        system_drive=effective_system_drive,
        path=target,
        path_identity=_path_identity(target),
    )


def begin_prepared_project_child_directory_use(
    pipeline: Any,
    *,
    project_root: Path,
    relative_path: Path | str,
    create: bool,
    system_drive: str | None = None,
) -> PreparedProjectChildDirectoryUse:
    project_use = begin_prepared_project_directory_use(
        pipeline,
        project_root=project_root,
        system_drive=system_drive,
    )
    parts = _safe_relative_parts(relative_path, label="project child directory")
    current = project_use.path
    identities: list[tuple[Path, tuple[int, int, int]]] = []
    for part in parts:
        project_use.verify()
        parent_identity = _path_identity(current)
        current = current / part
        if not _path_exists(current):
            if not create:
                raise ProjectIdentityError(
                    f"prepared project child must already exist: {current}"
                )
            try:
                current.mkdir(exist_ok=False)
            except OSError as exc:
                raise ProjectIdentityError(
                    f"cannot create prepared project child: {current}"
                ) from exc
            _require_path_identity(
                current.parent,
                parent_identity,
                label="prepared project child parent",
            )
        _require_plain_directory(current, label="prepared project child")
        identities.append((current, _path_identity(current)))
    child_use = PreparedProjectChildDirectoryUse(
        project_use=project_use,
        path=current,
        path_identities=tuple(identities),
    )
    child_use.verify()
    return child_use


def prepared_project_output_path(
    directory_use: PreparedProjectChildDirectoryUse,
    filename: str,
    *,
    must_exist: bool = False,
) -> Path:
    parts = _safe_relative_parts(filename, label="project output file")
    if len(parts) != 1:
        raise ProjectIdentityError("project output file must be directly inside its guarded directory")
    directory_use.verify()
    path = directory_use.path / parts[0]
    if _path_exists(path):
        _require_plain_regular_file(path, label="project output file")
    elif must_exist:
        raise ProjectIdentityError(f"project output file is missing: {path}")
    directory_use.verify()
    return path


def write_prepared_project_files(
    directory_use: PreparedProjectChildDirectoryUse,
    contents: dict[str, bytes],
) -> dict[str, Path]:
    if not contents:
        raise ProjectIdentityError("prepared project file batch must not be empty")
    final_paths: dict[str, Path] = {}
    originals: dict[str, bytes | None] = {}
    temporary_paths: dict[str, Path] = {}
    for filename, content in contents.items():
        if not isinstance(content, bytes):
            raise ProjectIdentityError("prepared project file content must be bytes")
        final_path = prepared_project_output_path(directory_use, filename)
        final_paths[filename] = final_path
        if _path_exists(final_path):
            identity = _path_identity(final_path)
            originals[filename] = final_path.read_bytes()
            _require_plain_regular_file(final_path, label="project output file")
            _require_path_identity(final_path, identity, label="project output file")
        else:
            originals[filename] = None

    try:
        for filename, content in contents.items():
            temporary_name = f".tmp-{secrets.token_hex(8)}"
            temporary_path = prepared_project_output_path(
                directory_use,
                temporary_name,
            )
            _write_exclusive_file(temporary_path, content)
            prepared_project_output_path(
                directory_use,
                temporary_name,
                must_exist=True,
            )
            temporary_paths[filename] = temporary_path

        for filename, final_path in final_paths.items():
            directory_use.verify()
            os.replace(temporary_paths[filename], final_path)
            prepared_project_output_path(
                directory_use,
                filename,
                must_exist=True,
            )
        directory_use.verify()
        return final_paths
    except BaseException as exc:
        for temporary_path in temporary_paths.values():
            try:
                if _path_exists(temporary_path):
                    _require_plain_regular_file(
                        temporary_path,
                        label="temporary project output file",
                    )
                    temporary_path.unlink()
            except (OSError, ProjectIdentityError):
                pass
        for filename, final_path in final_paths.items():
            original = originals[filename]
            try:
                if original is None:
                    if _path_exists(final_path):
                        _require_plain_regular_file(
                            final_path,
                            label="rollback project output file",
                        )
                        final_path.unlink()
                else:
                    rollback_path = directory_use.path / f".rb-{secrets.token_hex(8)}"
                    _write_exclusive_file(rollback_path, original)
                    os.replace(rollback_path, final_path)
            except (OSError, ProjectIdentityError):
                pass
        try:
            directory_use.verify()
        except ProjectIdentityError:
            pass
        if isinstance(exc, ProjectIdentityError):
            raise
        raise ProjectIdentityError(
            "prepared project file batch failed and was rolled back"
        ) from exc


def build_project_key(pipeline_id: int, display_title: str) -> str:
    pipeline_id = _require_pipeline_id(pipeline_id)
    prefix = f"p-{pipeline_id}-"
    available = min(MAX_PROJECT_SLUG_LENGTH, MAX_PROJECT_KEY_LENGTH - len(prefix))
    slug = project_slug_v1(display_title)
    slug = slug[:available].rstrip("-") or "project"
    return prefix + slug


def project_slug_v1(display_title: object) -> str:
    ascii_title = unicodedata.normalize("NFKD", str(display_title)).encode(
        "ascii", errors="ignore"
    ).decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_title.casefold()).strip("-")
    return slug[:MAX_PROJECT_SLUG_LENGTH].rstrip("-") or "project"


def build_pending_project_key(display_title: str) -> str:
    slug = build_project_key(1, display_title).removeprefix("p-1-")
    return "pending-" + secrets.token_hex(16) + "-" + slug


def sqlalchemy_pending_project_key(context: Any) -> str:
    parameters = context.get_current_parameters()
    return build_pending_project_key(str(parameters.get("project_name", "")))


def ensure_project_key(pipeline: Any) -> str:
    pipeline_id = _require_pipeline_id(getattr(pipeline, "id", None))
    existing = getattr(pipeline, "project_key", None)
    if existing in {None, ""}:
        assigned = build_project_key(
            pipeline_id,
            str(getattr(pipeline, "project_name", "")),
        )
        setattr(pipeline, "project_key", assigned)
        return assigned
    if not isinstance(existing, str) or not _valid_project_key_for_id(
        existing, pipeline_id
    ):
        raise ProjectIdentityError(
            f"project_key mismatch for pipeline {pipeline_id}: {existing!r}"
        )
    return existing


def add_pipeline_with_identity(session: Session, pipeline: Any) -> str:
    if getattr(pipeline, "id", None) is not None:
        raise ProjectIdentityError("new pipeline already has a persistent id")
    if getattr(pipeline, "project_key", None) not in {None, ""}:
        raise ProjectIdentityError("new pipeline already has a project_key")
    title = str(getattr(pipeline, "project_name", ""))
    # The safe slug is carried through the one INSERT statement so the 0002
    # database trigger can combine it with SQLite's assigned integer id.
    provisional = build_pending_project_key(title)
    setattr(pipeline, "project_key", provisional)
    session.add(pipeline)
    session.flush()
    pipeline_id = _require_pipeline_id(getattr(pipeline, "id", None))
    expected = build_project_key(pipeline_id, title)
    session.refresh(pipeline, attribute_names=["project_key"])
    canonical = getattr(pipeline, "project_key", None)
    if canonical != expected:
        raise ProjectIdentityError(
            "database project identity guard did not return the canonical key"
        )
    return expected


def write_project_directory_manifest(
    pipelines: Iterable[Any],
    *,
    project_root: Path,
    manifest_path: Path,
    system_drive: str | None = None,
) -> tuple[dict[str, Any], str]:
    root = require_non_system_drive(
        Path(project_root),
        system_drive=_effective_system_drive(system_drive),
    )
    manifest = Path(manifest_path)
    if not manifest.is_absolute():
        raise ValueError(f"manifest path must be absolute: {manifest}")
    _require_plain_directory(
        root,
        label="project root",
        system_drive=_effective_system_drive(system_drive),
    )
    _require_plain_directory(manifest.parent, label="manifest parent")

    projects: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    seen_keys: set[str] = set()
    for pipeline in pipelines:
        pipeline_id = _require_pipeline_id(getattr(pipeline, "id", None))
        project_key = ensure_project_key(pipeline)
        if pipeline_id in seen_ids or project_key in seen_keys:
            raise ProjectIdentityError(
                f"duplicate pipeline identity in directory manifest: {pipeline_id}, {project_key}"
            )
        seen_ids.add(pipeline_id)
        seen_keys.add(project_key)
        legacy = getattr(pipeline, "legacy_project_dir", None)
        if legacy is not None and not isinstance(legacy, str):
            raise ProjectIdentityError("legacy_project_dir must be an exact string or null")
        projects.append(
            {
                "pipeline_id": pipeline_id,
                "project_key": project_key,
                "target_directory": str(root / project_key),
                "legacy_project_dir": legacy,
            }
        )

    payload = {
        "manifest_format": PROJECT_DIRECTORY_MANIFEST_FORMAT,
        "manifest_path": str(manifest),
        "project_root": str(root),
        "projects": sorted(projects, key=lambda item: item["pipeline_id"]),
    }
    encoded = _canonical_json_bytes(payload)
    seal = hashlib.sha256(encoded).hexdigest()
    if _path_exists(manifest):
        _reject_reparse(manifest, label="project directory manifest")
        if not manifest.is_file():
            raise ProjectIdentityError(
                f"directory manifest collision or tampering: {manifest}"
            )
        try:
            existing = manifest.read_bytes()
        except OSError as exc:
            raise ProjectIdentityError(f"cannot read directory manifest: {manifest}") from exc
        if existing != encoded:
            raise ProjectIdentityError(f"directory manifest collision or tampering: {manifest}")
        return payload, seal
    try:
        _write_exclusive_file(manifest, encoded)
    except FileExistsError as exc:
        raise ProjectIdentityError(f"directory manifest collision: {manifest}") from exc
    except OSError as exc:
        raise ProjectIdentityError(f"cannot write directory manifest: {manifest}") from exc
    return payload, seal


def prepare_project_directories(
    manifest_path: Path,
    *,
    expected_sha256: str,
    system_drive: str | None = None,
) -> list[str]:
    manifest = Path(manifest_path)
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ProjectIdentityError("project directory manifest SHA256 seal is invalid")
    _reject_reparse_components(manifest, label="project directory manifest")
    manifest_identity = _path_identity(manifest)
    try:
        encoded = manifest.read_bytes()
    except OSError as exc:
        raise ProjectIdentityError(f"cannot read project directory manifest: {manifest}") from exc
    _require_path_identity(
        manifest,
        manifest_identity,
        label="project directory manifest",
    )
    actual_sha256 = hashlib.sha256(encoded).hexdigest()
    if not secrets.compare_digest(actual_sha256, expected_sha256):
        raise ProjectIdentityError("project directory manifest SHA256 seal mismatch or tampering")
    try:
        payload = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProjectIdentityError(f"cannot read project directory manifest: {manifest}") from exc
    if not isinstance(payload, dict) or payload.get("manifest_format") != (
        PROJECT_DIRECTORY_MANIFEST_FORMAT
    ):
        raise ProjectIdentityError("unknown or malformed project directory manifest")
    if payload.get("manifest_path") != str(manifest):
        raise ProjectIdentityError("project directory manifest path was tampered")

    root_value = payload.get("project_root")
    if not isinstance(root_value, str):
        raise ProjectIdentityError("project directory manifest has no project root")
    root = require_non_system_drive(
        Path(root_value),
        system_drive=_effective_system_drive(system_drive),
    )
    _require_plain_directory(
        root,
        label="project root",
        system_drive=_effective_system_drive(system_drive),
    )
    root_identity = _path_identity(root)
    raw_projects = payload.get("projects")
    if not isinstance(raw_projects, list):
        raise ProjectIdentityError("project directory manifest projects must be a list")

    plans: list[tuple[Path, dict[str, Any], bytes]] = []
    seen_ids: set[int] = set()
    seen_keys: set[str] = set()
    for entry in raw_projects:
        if not isinstance(entry, dict):
            raise ProjectIdentityError("project directory manifest entry is malformed")
        pipeline_id = _require_pipeline_id(entry.get("pipeline_id"))
        project_key = entry.get("project_key")
        if not isinstance(project_key, str) or not _valid_project_key_for_id(
            project_key, pipeline_id
        ):
            raise ProjectIdentityError("project directory manifest key is invalid")
        if pipeline_id in seen_ids or project_key in seen_keys:
            raise ProjectIdentityError("project directory manifest has duplicate identities")
        seen_ids.add(pipeline_id)
        seen_keys.add(project_key)
        target = root / project_key
        if entry.get("target_directory") != str(target):
            raise ProjectIdentityError("project target path was tampered")
        legacy = entry.get("legacy_project_dir")
        if legacy is not None and not isinstance(legacy, str):
            raise ProjectIdentityError("legacy_project_dir must remain a string or null")
        reference = {
            "reference_format": PROJECT_REFERENCE_FORMAT,
            "manifest_sha256": expected_sha256,
            "project_root": str(root),
            "target_directory": str(target),
            "pipeline_id": pipeline_id,
            "project_key": project_key,
            "legacy_project_dir": legacy,
        }
        plans.append((target, reference, _canonical_json_bytes(reference)))

    existing_identities: dict[
        Path,
        tuple[tuple[int, int, int], tuple[int, int, int]],
    ] = {}
    for target, _reference, reference_bytes in plans:
        _require_path_identity(root, root_identity, label="project root")
        if not _path_exists(target):
            continue
        _reject_reparse_components(target, label="project target directory")
        target_identity = _path_identity(target)
        reference_path = target / PROJECT_REFERENCE_FILENAME
        if not target.is_dir() or not _path_exists(reference_path):
            raise ProjectIdentityError(f"project directory collision: {target}")
        _reject_reparse_components(
            reference_path,
            label="project directory reference",
        )
        if not reference_path.is_file():
            raise ProjectIdentityError(f"project directory collision: {target}")
        reference_identity = _path_identity(reference_path)
        try:
            existing_reference = reference_path.read_bytes()
        except OSError as exc:
            raise ProjectIdentityError(
                f"project directory reference tampering: {reference_path}"
            ) from exc
        if existing_reference != reference_bytes:
            raise ProjectIdentityError(
                f"project directory reference tampering: {reference_path}"
            )
        _require_path_identity(target, target_identity, label="project target directory")
        _require_path_identity(
            reference_path,
            reference_identity,
            label="project directory reference",
        )
        existing_identities[target] = (target_identity, reference_identity)

    prepared: list[str] = []
    created_directories: list[tuple[Path, tuple[int, int, int]]] = []
    created_references: list[tuple[Path, tuple[int, int, int]]] = []
    try:
        for target, _reference, reference_bytes in plans:
            reference_path = target / PROJECT_REFERENCE_FILENAME
            _require_path_identity(root, root_identity, label="project root")
            _require_path_identity(
                manifest,
                manifest_identity,
                label="project directory manifest",
            )
            if not _path_exists(target):
                target.mkdir(exist_ok=False)
                _reject_reparse(target, label="created project target directory")
                _require_path_identity(root, root_identity, label="project root")
                target_identity = _path_identity(target)
                created_directories.append((target, target_identity))
                _require_path_identity(
                    target,
                    target_identity,
                    label="created project target directory",
                )
                reference_identity = _write_exclusive_file(
                    reference_path,
                    reference_bytes,
                )
                if reference_identity is None:
                    reference_identity = _path_identity(reference_path)
                created_references.append((reference_path, reference_identity))
                _require_path_identity(root, root_identity, label="project root")
                _require_path_identity(
                    target,
                    target_identity,
                    label="created project target directory",
                )
                _require_path_identity(
                    reference_path,
                    reference_identity,
                    label="created project directory reference",
                )
            else:
                expected_target, expected_reference = existing_identities[target]
                _require_path_identity(
                    target,
                    expected_target,
                    label="project target directory",
                )
                _require_path_identity(
                    reference_path,
                    expected_reference,
                    label="project directory reference",
                )
            prepared.append(str(target))
    except BaseException as exc:
        _cleanup_current_call(created_references, created_directories)
        if isinstance(exc, ProjectIdentityError):
            raise
        raise ProjectIdentityError(
            "project directory preparation or marker write failed"
        ) from exc
    return prepared


def prepare_pipeline_project_directory(
    pipeline: Any,
    *,
    project_root: Path,
    system_drive: str | None = None,
) -> Path:
    pipeline_id = _require_pipeline_id(getattr(pipeline, "id", None))
    project_key = ensure_project_key(pipeline)
    root = Path(project_root)
    canonical = root / project_key
    stored = getattr(pipeline, "project_dir", None)
    if stored not in {None, "", str(canonical)}:
        raise ProjectIdentityError(
            f"stored project_dir is not canonical for pipeline {pipeline_id}"
        )
    manifest_path = root / f".project-directory-plan-{pipeline_id}.json"
    _payload, seal = write_project_directory_manifest(
        [pipeline],
        project_root=root,
        manifest_path=manifest_path,
        system_drive=system_drive,
    )
    [prepared] = prepare_project_directories(
        manifest_path,
        expected_sha256=seal,
        system_drive=system_drive,
    )
    setattr(pipeline, "project_dir", prepared)
    return resolve_prepared_project_directory(
        pipeline,
        project_root=root,
        system_drive=system_drive,
    )


def resolve_prepared_project_directory(
    pipeline: Any,
    *,
    project_root: Path,
    system_drive: str | None = None,
) -> Path:
    root = require_non_system_drive(
        Path(project_root),
        system_drive=_effective_system_drive(system_drive),
    )
    _require_plain_directory(
        root,
        label="approved project root",
        system_drive=_effective_system_drive(system_drive),
    )
    root_identity = _path_identity(root)
    pipeline_id = _require_pipeline_id(getattr(pipeline, "id", None))
    project_key = ensure_project_key(pipeline)
    target = root / project_key
    stored = getattr(pipeline, "project_dir", None)
    if stored != str(target):
        raise ProjectIdentityError(
            f"stored project_dir is not canonical for pipeline {pipeline_id}: {stored!r}"
        )
    _require_plain_directory(target, label="prepared project target")
    target_identity = _path_identity(target)
    reference_path = target / PROJECT_REFERENCE_FILENAME
    _reject_reparse_components(
        reference_path,
        label="project directory reference marker",
    )
    _require_plain_regular_file(
        reference_path,
        label="project directory reference marker",
    )
    reference_identity = _path_identity(reference_path)
    try:
        reference_bytes = reference_path.read_bytes()
        reference = json.loads(reference_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProjectIdentityError(
            f"project directory reference marker is unreadable: {reference_path}"
        ) from exc
    if not isinstance(reference, dict) or reference_bytes != _canonical_json_bytes(
        reference
    ):
        raise ProjectIdentityError("project directory reference marker was tampered")
    expected_fields = {
        "reference_format",
        "manifest_sha256",
        "project_root",
        "target_directory",
        "pipeline_id",
        "project_key",
        "legacy_project_dir",
    }
    if set(reference) != expected_fields:
        raise ProjectIdentityError("project directory reference marker was tampered")
    if (
        reference.get("reference_format") != PROJECT_REFERENCE_FORMAT
        or not isinstance(reference.get("manifest_sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", str(reference["manifest_sha256"])) is None
        or reference.get("project_root") != str(root)
        or reference.get("target_directory") != str(target)
        or reference.get("pipeline_id") != pipeline_id
        or reference.get("project_key") != project_key
        or reference.get("legacy_project_dir")
        != getattr(pipeline, "legacy_project_dir", None)
    ):
        raise ProjectIdentityError("project directory reference marker was tampered")
    manifest_path, manifest_identity = _validate_authoritative_runtime_manifest(
        pipeline=pipeline,
        root=root,
        target=target,
        pipeline_id=pipeline_id,
        project_key=project_key,
        expected_sha256=str(reference["manifest_sha256"]),
    )
    _require_path_identity(root, root_identity, label="approved project root")
    _require_path_identity(target, target_identity, label="prepared project target")
    _require_path_identity(
        reference_path,
        reference_identity,
        label="project directory reference marker",
    )
    _require_path_identity(
        manifest_path,
        manifest_identity,
        label="authoritative project directory manifest",
    )
    return target


def _validate_authoritative_runtime_manifest(
    *,
    pipeline: Any,
    root: Path,
    target: Path,
    pipeline_id: int,
    project_key: str,
    expected_sha256: str,
) -> tuple[Path, tuple[int, int, int]]:
    manifest_path = root / f".project-directory-plan-{pipeline_id}.json"
    try:
        _require_plain_regular_file(
            manifest_path,
            label="authoritative project directory manifest",
        )
        manifest_identity = _path_identity(manifest_path)
        encoded = manifest_path.read_bytes()
    except OSError as exc:
        raise ProjectIdentityError(
            f"authoritative project directory manifest is unreadable: {manifest_path}"
        ) from exc
    actual_sha256 = hashlib.sha256(encoded).hexdigest()
    if not secrets.compare_digest(actual_sha256, expected_sha256):
        raise ProjectIdentityError(
            "authoritative project directory manifest seal mismatch or tampering"
        )
    try:
        payload = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProjectIdentityError(
            "authoritative project directory manifest is malformed"
        ) from exc
    legacy = getattr(pipeline, "legacy_project_dir", None)
    expected_payload = {
        "manifest_format": PROJECT_DIRECTORY_MANIFEST_FORMAT,
        "manifest_path": str(manifest_path),
        "project_root": str(root),
        "projects": [
            {
                "pipeline_id": pipeline_id,
                "project_key": project_key,
                "target_directory": str(target),
                "legacy_project_dir": legacy,
            }
        ],
    }
    if payload != expected_payload or encoded != _canonical_json_bytes(expected_payload):
        raise ProjectIdentityError(
            "authoritative project directory manifest was tampered"
        )
    _require_path_identity(
        manifest_path,
        manifest_identity,
        label="authoritative project directory manifest",
    )
    return manifest_path, manifest_identity


def _canonical_json_bytes(payload: dict[str, Any]) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _effective_system_drive(system_drive: str | None) -> str:
    if system_drive is not None:
        return system_drive
    configured = os.environ.get("SystemDrive")
    if not configured:
        raise ProjectIdentityError(
            "SystemDrive is unavailable; an explicit system drive is required"
        )
    return configured


def _path_exists(path: Path) -> bool:
    return os.path.lexists(path)


def _safe_relative_parts(value: Path | str, *, label: str) -> tuple[str, ...]:
    candidate = Path(value)
    if candidate.is_absolute() or candidate.drive:
        raise ProjectIdentityError(f"{label} must be relative")
    parts = tuple(str(part) for part in candidate.parts)
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ProjectIdentityError(f"{label} is invalid")
    return parts


def _is_reparse(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    reparse_attribute = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    is_junction = getattr(os.path, "isjunction", None)
    return bool(
        path.is_symlink()
        or attributes & reparse_attribute
        or (callable(is_junction) and is_junction(path))
    )


def _reject_reparse(path: Path, *, label: str) -> None:
    if _is_reparse(path):
        raise ProjectIdentityError(
            f"{label} must not be a symlink, junction, or reparse point: {path}"
        )


def _reject_reparse_components(path: Path, *, label: str) -> None:
    candidate = Path(path)
    for component in (candidate, *candidate.parents):
        if _path_exists(component) and _is_reparse(component):
            raise ProjectIdentityError(
                f"{label} has a symlink, junction, or reparse ancestor: {component}"
            )


def _require_plain_directory(
    path: Path,
    *,
    label: str,
    system_drive: str | None = None,
) -> None:
    if not _path_exists(path):
        raise ProjectIdentityError(f"{label} must already exist: {path}")
    _reject_reparse_components(path, label=label)
    if not path.is_dir():
        raise ProjectIdentityError(f"{label} must be a directory: {path}")
    lexical = Path(os.path.abspath(path))
    physical = path.resolve(strict=True)
    if os.path.normcase(str(lexical)) != os.path.normcase(str(physical)):
        raise ProjectIdentityError(
            f"{label} lexical and physical paths differ: {lexical} != {physical}"
        )
    if system_drive is not None:
        require_non_system_drive(physical, system_drive=system_drive)


def _require_plain_regular_file(path: Path, *, label: str) -> None:
    if not _path_exists(path):
        raise ProjectIdentityError(f"{label} must already exist: {path}")
    _reject_reparse_components(path, label=label)
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise ProjectIdentityError(f"{label} must be a regular file: {path}")
    if int(metadata.st_nlink) != 1:
        raise ProjectIdentityError(f"{label} must not be a hard link: {path}")


def _path_identity(path: Path) -> tuple[int, int, int]:
    metadata = path.lstat()
    return int(metadata.st_dev), int(metadata.st_ino), stat.S_IFMT(metadata.st_mode)


def _require_path_identity(
    path: Path,
    expected: tuple[int, int, int],
    *,
    label: str,
) -> None:
    _reject_reparse_components(path, label=label)
    try:
        actual = _path_identity(path)
    except OSError as exc:
        raise ProjectIdentityError(f"{label} disappeared during preparation: {path}") from exc
    if actual != expected:
        raise ProjectIdentityError(f"{label} identity changed during preparation: {path}")


def _write_exclusive_file(path: Path, content: bytes) -> tuple[int, int, int]:
    created_identity: tuple[int, int, int] | None = None
    try:
        with path.open("xb") as output:
            created_identity = (
                int(os.fstat(output.fileno()).st_dev),
                int(os.fstat(output.fileno()).st_ino),
                stat.S_IFMT(os.fstat(output.fileno()).st_mode),
            )
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        if created_identity is not None:
            try:
                if _path_exists(path) and _path_identity(path) == created_identity:
                    path.unlink()
            except OSError:
                pass
        raise
    assert created_identity is not None
    return created_identity


def _cleanup_current_call(
    references: list[tuple[Path, tuple[int, int, int]]],
    directories: list[tuple[Path, tuple[int, int, int]]],
) -> None:
    for reference, identity in reversed(references):
        try:
            if _path_exists(reference) and _path_identity(reference) == identity:
                reference.unlink()
        except OSError:
            pass
    for directory, identity in reversed(directories):
        try:
            if _path_exists(directory) and _path_identity(directory) == identity:
                directory.rmdir()
        except OSError:
            pass


def _require_pipeline_id(value: Any) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value <= 0
        or value > 2**63 - 1
    ):
        raise ProjectIdentityError(f"pipeline id must be a positive integer: {value!r}")
    return value


def _valid_project_key_for_id(project_key: str, pipeline_id: int) -> bool:
    if (
        isinstance(pipeline_id, bool)
        or not isinstance(pipeline_id, int)
        or pipeline_id <= 0
        or pipeline_id > 2**63 - 1
        or not isinstance(project_key, str)
    ):
        return False
    prefix = f"p-{pipeline_id}-"
    if not project_key.startswith(prefix):
        return False
    slug = project_key[len(prefix) :]
    return (
        len(project_key) <= MAX_PROJECT_KEY_LENGTH
        and _PROJECT_KEY_PATTERN.fullmatch(project_key) is not None
        and 1 <= len(slug) <= MAX_PROJECT_SLUG_LENGTH
        and _PROJECT_SLUG_PATTERN.fullmatch(slug) is not None
    )
