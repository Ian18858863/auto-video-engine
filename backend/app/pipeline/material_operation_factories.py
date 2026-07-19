"""Server-owned factories for the contained material-operation protocol.

The old material executor accepts a large mutable task dictionary and is kept
behind the legacy endpoint.  This module is the small adapter for the new
owner-lane seam: it reads only an already detached, server-fresh material
snapshot and returns a :class:`ProductionMaterialOperationPlan` containing
strict request/secret payloads.

No directory is created here.  The runner owns physical root approval and
attempt-directory creation after the claim has been acquired.  In particular,
client paths, output paths, API keys, ORM objects and source-chain decisions do
not cross this boundary.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
import re
from typing import Any

from sqlalchemy.engine import Connection, Engine

from app.core.database import get_sqlite_database_path
from app.materials.asset_paths import AutoMaterialPaths
from app.materials.paths import PROJECT_DIRS
from app.models.entities import PipelineStep, ProductionPipeline
from app.pipeline.material_execution_application import MaterialExecutionCommand
from app.pipeline.material_operation_protocol import (
    CoverrSecrets,
    InfoMotionPayload,
    InfoMotionSecrets,
    MaterialOperationKind,
    MaterialSecretPayload,
    OfficialCapturePayload,
    OfficialCaptureSecrets,
    PexelsSecrets,
    PixabaySecrets,
    StockCandidateSecrets,
    StockSearchPayload,
)
from app.pipeline.project_identity import (
    PreparedPipelineIdentity,
    begin_prepared_project_child_directory_use,
)
from app.pipeline.production_material_execution import (
    ProductionMaterialOperationPlan,
    ProductionMaterialPublicationRecoveryContext,
)


class MaterialOperationFactoryError(ValueError):
    """A deterministic, pre-claim operation-plan rejection."""


SecretLoader = Callable[[MaterialOperationKind, AutoMaterialPaths], MaterialSecretPayload]
CleanupFactory = Callable[[MaterialExecutionCommand, Mapping[str, Any]], Callable[[], None] | None]
ProjectDirectoryUseFactory = Callable[
    [MaterialExecutionCommand, Mapping[str, Any]],
    Mapping[str, Any],
]

_MAX_PER_PAGE = 100
_DEFAULT_PER_PAGE = 10
_SUPPORTED_OPERATIONS = frozenset(
    {
        "official_capture",
        "pexels",
        "pixabay",
        "coverr",
        "info_motion",
        "stock_candidates",
    }
)
_CLIENT_CONTROL_KEYS = frozenset(
    {
        "database_path",
        "output_directory",
        "output_dir",
        "project_directory",
        "project_dir",
        "canonical_root",
        "owner_token",
    }
)
_WINDOWS_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]|^[\\/]{2}|(?:^|[\\/])\.\.?(?:[\\/]|$)")


def _normalise_material_root(value: Path | str) -> Path:
    if isinstance(value, Path):
        path = value
    elif isinstance(value, str) and value.strip():
        path = Path(value)
    else:
        raise MaterialOperationFactoryError("material_root must be a path")
    if not path.is_absolute():
        raise MaterialOperationFactoryError("material_root must be an absolute D path")
    resolved = path.resolve(strict=False)
    if resolved.drive.casefold() != "d:" or str(resolved).startswith("\\\\"):
        raise MaterialOperationFactoryError("material_root must remain on D:")
    return resolved


def _bound_engine(bind: Any) -> Engine:
    if isinstance(bind, Engine):
        return bind
    if isinstance(bind, Connection):
        return bind.engine
    candidate = getattr(bind, "engine", None)
    if isinstance(candidate, Engine):
        return candidate
    raise MaterialOperationFactoryError(
        "project directory identity session has an unsupported database bind"
    )


class SqlAlchemyProjectDirectoryUseFactory:
    """Fresh-read immutable project guards for one owner-lane plan.

    Both the database file and project root are injected by server assembly.
    Neither value is accepted from the material task or detached JSON snapshot.
    """

    def __init__(
        self,
        *,
        session_factory: Callable[[], Any],
        expected_database_path: Path | str,
        project_root: Path | str,
    ) -> None:
        if not callable(session_factory):
            raise TypeError("session_factory must be callable")
        self._session_factory = session_factory
        self._expected_database_path = _normalise_material_root(
            expected_database_path
        )
        self._project_root = _normalise_material_root(project_root)

    @property
    def project_root(self) -> Path:
        return self._project_root

    def __call__(
        self,
        command: MaterialExecutionCommand,
        task: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if type(command) is not MaterialExecutionCommand:
            raise TypeError("command must be MaterialExecutionCommand")
        if not isinstance(task, Mapping):
            raise TypeError("task must be a mapping")
        session: Any | None = None
        try:
            session = self._session_factory()
            if session is None:
                raise TypeError("session_factory returned None")
            actual_database_path = get_sqlite_database_path(
                _bound_engine(session.get_bind())
            ).resolve(strict=False)
            if (
                str(actual_database_path).casefold()
                != str(self._expected_database_path).casefold()
            ):
                raise MaterialOperationFactoryError(
                    "project directory identity session is bound to another database"
                )
            rollback = getattr(session, "rollback", None)
            if callable(rollback):
                rollback()
            expire_all = getattr(session, "expire_all", None)
            if callable(expire_all):
                expire_all()
            pipeline = session.get(ProductionPipeline, command.pipeline_id)
            step = session.get(PipelineStep, command.material_step_id)
            if pipeline is None or step is None:
                raise MaterialOperationFactoryError(
                    "project directory identity pipeline or material step is missing"
                )
            if step.pipeline_id != pipeline.id:
                raise MaterialOperationFactoryError(
                    "project directory identity material step belongs to another pipeline"
                )
            # Require the independently fresh DB step to contain the same task.
            # This is an identity check only; no task path or project path is used.
            _task_from_snapshot(step.output_summary or {}, command.material_task_id)
            identity = PreparedPipelineIdentity.from_pipeline(pipeline)
        except MaterialOperationFactoryError:
            raise
        except Exception as exc:
            raise MaterialOperationFactoryError(
                "server project directory identity is unavailable"
            ) from exc
        finally:
            if session is not None:
                close = getattr(session, "close", None)
                if callable(close):
                    close()

        return {
            dirname: begin_prepared_project_child_directory_use(
                identity,
                project_root=self._project_root,
                relative_path=dirname,
                create=False,
            )
            for dirname in PROJECT_DIRS
        }


def _text(value: Any, *, label: str, required: bool = False, maximum: int = 1024) -> str:
    if not isinstance(value, str):
        if required:
            raise MaterialOperationFactoryError(f"{label} is required")
        return ""
    result = value.strip()
    if required and not result:
        raise MaterialOperationFactoryError(f"{label} is required")
    if len(result) > maximum:
        raise MaterialOperationFactoryError(f"{label} is too long")
    return result


def _task_from_snapshot(
    fresh_payload: Mapping[str, Any],
    material_task_id: str,
) -> Mapping[str, Any]:
    if not isinstance(fresh_payload, Mapping):
        raise MaterialOperationFactoryError("fresh material snapshot must be an object")
    forbidden = _CLIENT_CONTROL_KEYS.intersection(fresh_payload.keys())
    if forbidden:
        raise MaterialOperationFactoryError(
            "fresh material snapshot does not accept client-controlled identity or path fields"
        )
    raw_tasks = fresh_payload.get("material_tasks")
    if not isinstance(raw_tasks, (list, tuple)):
        raise MaterialOperationFactoryError("fresh material snapshot has no material task list")
    matches = tuple(
        item
        for item in raw_tasks
        if isinstance(item, Mapping)
        and item.get("material_task_id") == material_task_id
    )
    if not matches:
        raise MaterialOperationFactoryError(
            "requested material task is not present in the fresh snapshot"
        )
    if len(matches) != 1:
        raise MaterialOperationFactoryError(
            "fresh material snapshot requires exactly one matching material_task_id"
        )
    task = matches[0]
    forbidden = _CLIENT_CONTROL_KEYS.intersection(task.keys())
    if forbidden:
        raise MaterialOperationFactoryError(
            "fresh material task does not accept client-controlled identity or path fields"
        )
    return task


def _server_text(task: Mapping[str, Any], *keys: str, required: bool = False) -> str:
    for key in keys:
        value = _text(task.get(key), label=key)
        if value:
            return value
    if required:
        raise MaterialOperationFactoryError(f"missing server material field: {keys[0]}")
    return ""


def _server_label(task: Mapping[str, Any], *keys: str) -> str:
    """Read a server-owned display label and reject path-shaped values."""

    value = _server_text(task, *keys)
    if value and (
        _WINDOWS_PATH_RE.search(value)
        or "/" in value
        or "\\" in value
        or any(ord(character) < 32 for character in value)
    ):
        raise MaterialOperationFactoryError(
            "server material label must not be a path or control string"
        )
    return value


def _safe_keyword_values(task: Mapping[str, Any]) -> tuple[str, ...]:
    values: list[str] = []
    for key in ("asset_keywords", "jianying_keywords"):
        raw = task.get(key)
        if isinstance(raw, str):
            raw_values: Sequence[Any] = (raw,)
        elif isinstance(raw, (list, tuple)):
            raw_values = raw
        else:
            raw_values = ()
        for item in raw_values:
            if not isinstance(item, str):
                continue
            value = item.strip()
            # A path is never a search keyword, even if a legacy task happened
            # to persist one under an overly broad keyword field.
            if value and not _WINDOWS_PATH_RE.search(value):
                values.append(value)
    for key in ("recommended_visual", "voiceover"):
        value = _text(task.get(key), label=key, maximum=2048)
        if value and not _WINDOWS_PATH_RE.search(value):
            values.append(value)
    return tuple(values)


def _stock_query(task: Mapping[str, Any]) -> str:
    text = " ".join(_safe_keyword_values(task))
    mapping = (
        ("AI", "artificial intelligence technology office"),
        ("工具", "software productivity office"),
        ("tool", "software productivity office"),
        ("科技", "technology office"),
        ("technology", "technology office"),
        ("城市", "city office"),
        ("city", "city office"),
        ("办公", "modern office"),
        ("office", "modern office"),
        ("商业", "business meeting"),
        ("business", "business meeting"),
        ("会议", "business meeting"),
        ("财经", "financial news office"),
        ("数据", "data analysis dashboard"),
        ("data", "data analysis dashboard"),
        ("消费", "shopping consumer lifestyle"),
        ("企业", "business team office"),
        ("采购", "business procurement meeting"),
        ("效率", "productivity office"),
    )
    selected: list[str] = []
    folded = text.casefold()
    for token, query in mapping:
        if token.casefold() in folded and query not in selected:
            selected.append(query)
    return " ".join(selected[:2]) or "technology business office"


def _assert_stock_candidate_refresh_is_unlocked(
    fresh_payload: Mapping[str, Any],
    task: Mapping[str, Any],
) -> None:
    """Reject a generation refresh that could overwrite a locked choice.

    This pre-claim check intentionally inspects only persisted structure.  It
    does not trust preview paths or perform filesystem I/O; the lifecycle CAS
    repeats the authoritative locked-row check immediately before writeback.
    """

    raw_pool = fresh_payload.get("candidate_pool")
    if raw_pool is None:
        return
    if not isinstance(raw_pool, Mapping):
        raise MaterialOperationFactoryError("candidate_pool must be an object")
    shot_id = _server_text(task, "shot_id", required=True)
    if "candidates" in raw_pool or "shot_id" in raw_pool:
        target = raw_pool
        declared_shot_id = target.get("shot_id")
        if declared_shot_id != shot_id:
            return
    else:
        target = raw_pool.get(shot_id)
        if target is None:
            return
        if not isinstance(target, Mapping):
            raise MaterialOperationFactoryError(
                "target candidate_pool entry must be an object"
            )
        declared_shot_id = target.get("shot_id")
        if declared_shot_id not in {None, shot_id}:
            raise MaterialOperationFactoryError(
                "target candidate_pool entry belongs to another shot"
            )
    candidates = target.get("candidates")
    if not isinstance(candidates, (list, tuple)):
        raise MaterialOperationFactoryError(
            "target candidate_pool entry has no candidate list"
        )
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            raise MaterialOperationFactoryError(
                "target candidate_pool contains an invalid candidate"
            )
        status = candidate.get("status")
        if not isinstance(status, str) or not status.strip():
            raise MaterialOperationFactoryError(
                "target candidate_pool contains an invalid candidate status"
            )
        normalised = status.casefold().replace("-", "_").replace(" ", "_")
        if normalised in {"locked", "selected"}:
            raise MaterialOperationFactoryError(
                "stock candidate refresh cannot overwrite a locked candidate pool"
            )


def _per_page(operation: MaterialOperationKind, configured: Mapping[str, int]) -> int:
    value = configured.get(operation, _DEFAULT_PER_PAGE)
    if type(value) is not int or not 1 <= value <= _MAX_PER_PAGE:
        raise MaterialOperationFactoryError("per_page must be an integer from 1 to 100")
    return value


def _default_secret_loader(
    operation: MaterialOperationKind,
    paths: AutoMaterialPaths,
) -> MaterialSecretPayload:
    try:
        if operation == "pexels":
            return PexelsSecrets(paths.read_pexels_api_key())
        if operation == "pixabay":
            return PixabaySecrets(paths.read_pixabay_api_key())
        if operation == "stock_candidates":
            return StockCandidateSecrets(
                pexels_api_key=paths.read_pexels_api_key(),
                pixabay_api_key=paths.read_pixabay_api_key(),
            )
        if operation == "official_capture":
            return OfficialCaptureSecrets()
        if operation == "coverr":
            return CoverrSecrets()
        if operation == "info_motion":
            return InfoMotionSecrets()
    except Exception as exc:
        # Do not carry a secret-file path or provider exception into a public
        # response.  The chained exception remains available to the owner lane
        # logger, whose policy can redact it separately.
        raise MaterialOperationFactoryError("server material secret is unavailable") from exc
    raise MaterialOperationFactoryError("material operation is not allowed")


def _expected_secret_type(operation: MaterialOperationKind) -> type:
    return {
        "official_capture": OfficialCaptureSecrets,
        "pexels": PexelsSecrets,
        "pixabay": PixabaySecrets,
        "stock_candidates": StockCandidateSecrets,
        "coverr": CoverrSecrets,
        "info_motion": InfoMotionSecrets,
    }[operation]


class ServerMaterialOperationPlanFactory:
    """Build strict operation plans from one server-owned fresh snapshot."""

    def __init__(
        self,
        *,
        material_root: Path | str,
        secret_loader: SecretLoader | None = None,
        per_page: Mapping[str, int] | None = None,
        allow_coverr: bool = False,
        cleanup_factory: CleanupFactory | None = None,
        project_directory_use_factory: ProjectDirectoryUseFactory | None = None,
    ) -> None:
        self._material_root = _normalise_material_root(material_root)
        if secret_loader is not None and not callable(secret_loader):
            raise TypeError("secret_loader must be callable")
        self._secret_loader = secret_loader or _default_secret_loader
        self._per_page = dict(per_page or {})
        self._allow_coverr = bool(allow_coverr)
        if cleanup_factory is not None and not callable(cleanup_factory):
            raise TypeError("cleanup_factory must be callable")
        self._cleanup_factory = cleanup_factory
        if project_directory_use_factory is not None and not callable(
            project_directory_use_factory
        ):
            raise TypeError("project_directory_use_factory must be callable")
        self._project_directory_use_factory = project_directory_use_factory

    @property
    def secret_loader(self) -> SecretLoader:
        """Return the injected server-only loader (never a loaded secret)."""

        return self._secret_loader

    @property
    def material_root(self) -> Path:
        return self._material_root

    def with_cleanup(self, callback: Callable[[], None]) -> "ServerMaterialOperationPlanFactory":
        if not callable(callback):
            raise TypeError("cleanup callback must be callable")
        return ServerMaterialOperationPlanFactory(
            material_root=self._material_root,
            secret_loader=self._secret_loader,
            per_page=self._per_page,
            allow_coverr=self._allow_coverr,
            cleanup_factory=lambda _command, _task: callback,
            project_directory_use_factory=self._project_directory_use_factory,
        )

    def build_publication_recovery_context(
        self,
        command: Any,
        fresh_payload: Mapping[str, Any],
    ) -> ProductionMaterialPublicationRecoveryContext:
        """Build existing publication guards without loading provider secrets."""

        from app.pipeline.material_execution_application import (
            MaterialRestartRecoveryCommand,
            MaterialRunnerRetirementRecoveryCommand,
        )

        if type(command) not in {
            MaterialRestartRecoveryCommand,
            MaterialRunnerRetirementRecoveryCommand,
        }:
            raise TypeError("command must be a supported material recovery command")
        if command.operation not in _SUPPORTED_OPERATIONS:
            raise MaterialOperationFactoryError("material operation is not allowed")
        task = _task_from_snapshot(fresh_payload, command.material_task_id)
        startup_timeout_seconds = getattr(command, "startup_timeout_seconds", 1.0)
        normal_command = MaterialExecutionCommand(
            pipeline_id=command.pipeline_id,
            material_step_id=command.material_step_id,
            material_task_id=command.material_task_id,
            execution_id=command.execution_id,
            operation=command.operation,
            startup_timeout_seconds=startup_timeout_seconds,
            operation_timeout_seconds=max(startup_timeout_seconds, 1.0),
        )
        project_directory_uses: dict[str, Any] = {}
        if self._project_directory_use_factory is not None:
            try:
                supplied = self._project_directory_use_factory(normal_command, task)
            except MaterialOperationFactoryError:
                raise
            except Exception as exc:
                raise MaterialOperationFactoryError(
                    "server project directory identity is unavailable"
                ) from exc
            if not isinstance(supplied, Mapping):
                raise MaterialOperationFactoryError(
                    "project directory use factory returned a non-mapping"
                )
            for key, value in supplied.items():
                if not isinstance(key, str) or not key.strip():
                    raise MaterialOperationFactoryError(
                        "project directory guard names must be non-empty strings"
                    )
                project_directory_uses[key] = value
        return ProductionMaterialPublicationRecoveryContext(
            canonical_paths=AutoMaterialPaths(root=self._material_root),
            project_directory_uses=project_directory_uses,
        )

    def __call__(
        self,
        command: MaterialExecutionCommand,
        fresh_payload: Mapping[str, Any],
    ) -> ProductionMaterialOperationPlan:
        if type(command) is not MaterialExecutionCommand:
            raise TypeError("command must be MaterialExecutionCommand")
        if command.operation not in _SUPPORTED_OPERATIONS:
            raise MaterialOperationFactoryError("material operation is not allowed")
        if command.operation == "coverr" and not self._allow_coverr:
            raise MaterialOperationFactoryError(
                "Coverr requires explicit server-side opt-in before execution"
            )
        task = _task_from_snapshot(fresh_payload, command.material_task_id)
        if command.operation == "stock_candidates":
            _assert_stock_candidate_refresh_is_unlocked(fresh_payload, task)
        paths = AutoMaterialPaths(root=self._material_root)

        if command.operation == "official_capture":
            request_payload = OfficialCapturePayload(
                source_url=_server_text(task, "official_source_url", "source_url", required=True),
                source_name=_server_text(
                    task,
                    "official_source_name",
                    "source_platform",
                    "source_type",
                    required=True,
                ),
                page_title=_server_text(
                    task,
                    "official_page_title",
                    "recommended_visual",
                    "voiceover",
                    required=True,
                ),
            )
        elif command.operation in {
            "pexels",
            "pixabay",
            "coverr",
            "stock_candidates",
        }:
            request_payload = StockSearchPayload(
                query=_stock_query(task),
                per_page=_per_page(command.operation, self._per_page),
            )
        else:
            # New material-step snapshots persist these values at the top
            # level, where they are generated from the authoritative pipeline
            # row.  The task-level value remains a compatibility form for old
            # snapshots.  A deterministic pipeline-id fallback keeps legacy
            # rows executable without ever accepting a client path or name.
            project_name = _server_label(
                fresh_payload,
                "project_name",
                "project_key",
            )
            if not project_name:
                project_name = _server_label(task, "project_name", "project_key")
            if not project_name:
                project_name = f"pipeline-{command.pipeline_id}"
            request_payload = InfoMotionPayload(
                project_name=project_name,
                title=_server_text(
                    task,
                    "recommended_visual",
                    "voiceover",
                    "project_name",
                    required=True,
                ),
            )

        try:
            secret_payload = self._secret_loader(command.operation, paths)
        except MaterialOperationFactoryError:
            raise
        except Exception as exc:
            raise MaterialOperationFactoryError("server material secret is unavailable") from exc
        if type(secret_payload) is not _expected_secret_type(command.operation):
            raise MaterialOperationFactoryError(
                "server material secret type does not match the operation"
            )

        cleanup = None
        if self._cleanup_factory is not None:
            cleanup = self._cleanup_factory(command, task)
            if cleanup is not None and not callable(cleanup):
                raise MaterialOperationFactoryError("cleanup factory returned a non-callable")
        project_directory_uses: dict[str, Any] = {}
        if self._project_directory_use_factory is not None:
            try:
                supplied = self._project_directory_use_factory(command, task)
            except MaterialOperationFactoryError:
                raise
            except Exception as exc:
                raise MaterialOperationFactoryError(
                    "server project directory identity is unavailable"
                ) from exc
            if not isinstance(supplied, Mapping):
                raise MaterialOperationFactoryError(
                    "project directory use factory returned a non-mapping"
                )
            for key, value in supplied.items():
                if not isinstance(key, str) or not key.strip():
                    raise MaterialOperationFactoryError(
                        "project directory guard names must be non-empty strings"
                    )
                project_directory_uses[key] = value
        return ProductionMaterialOperationPlan(
            request_payload=request_payload,
            secret_payload=secret_payload,
            canonical_paths=paths,
            project_directory_uses=project_directory_uses,
            cleanup=cleanup,
        )


# Short aliases for callers that prefer the domain term used by the proposal.
MaterialOperationPlanFactory = ServerMaterialOperationPlanFactory
build_material_operation_plan_factory = ServerMaterialOperationPlanFactory


__all__ = [
    "MaterialOperationFactoryError",
    "MaterialOperationPlanFactory",
    "ProjectDirectoryUseFactory",
    "ServerMaterialOperationPlanFactory",
    "SqlAlchemyProjectDirectoryUseFactory",
    "build_material_operation_plan_factory",
]
