"""Crash-matrix controller fixture for the Task 3B runner.

This process intentionally blocks after one durable boundary.  The parent test
terminates only this controller; it never opens the controller's Job handle.
The controller's native Job handle therefore closes as a consequence of the
process exit and the Job's KILL_ON_CLOSE policy owns worker containment.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import os
from pathlib import Path
import time
from types import MappingProxyType
from typing import Callable

# The crash controller is launched with the approved runtime's base
# executable, rather than the venv redirector.  Keep pywin32's DLL directory
# loaded before importing any module that imports win32api/win32job.
_SITE_PACKAGES = Path(__file__).resolve().parents[2] / ".venv" / "Lib" / "site-packages"
_PYWIN32_DLL_DIRECTORY = (
    os.add_dll_directory(str(_SITE_PACKAGES / "pywin32_system32"))
    if os.name == "nt"
    else None
)

from app.core.windows_durable_file import write_durable_json
from app.pipeline import windows_atomic_process as atomic_subject
from app.pipeline import windows_job_object as job_subject
from app.pipeline import windows_overlapped_pipe_io as pipe_subject
from app.pipeline import material_operation_runner as runner_subject
from app.pipeline.material_operation_protocol import (
    MaterialOperationRequest,
    MaterialOperationSecrets,
    PexelsSecrets,
    StockSearchPayload,
)
from app.pipeline.material_operation_runner import (
    MaterialLaunchPhase,
    PreparedMaterialAttempt,
    ProcessMaterialOperationRunner,
    _WorkerLaunchSpec,
    _WorkerRuntimePreflight,
)


_EXECUTION_ID = "123e4567-e89b-12d3-a456-426614174000"
_CHECKPOINT_MAX_BYTES = 64 * 1024
_WORKER_SECRET = "task-3b-crash-matrix-secret"


def _sleep_until_terminated() -> None:
    while True:
        time.sleep(0.25)


class _CrashRunner(ProcessMaterialOperationRunner):
    def __init__(
        self,
        *,
        root: Path,
        target_phase: str | None,
        boundary: str | None,
        checkpoint_name: str,
    ) -> None:
        super().__init__(root=root)
        self.target_phase = target_phase
        self.boundary = boundary
        self.checkpoint_name = checkpoint_name
        self.prepared: PreparedMaterialAttempt | None = None
        self.live_root_pid: int | None = None
        self.raw_query_root_pid: int | None = None

    def _checkpoint(
        self,
        *,
        label: str,
        root_pid: int | None = None,
        root_creation_time: str | None = None,
    ) -> None:
        prepared = self.prepared
        if prepared is None:
            raise RuntimeError("crash checkpoint has no prepared attempt")
        descriptor_bytes = (
            prepared.control_directory.path / "container.json"
        ).read_bytes()
        descriptor = runner_subject.decode_material_container_descriptor(
            descriptor_bytes
        )
        if root_pid is None:
            root_pid = descriptor.root_pid
        if root_creation_time is None:
            root_creation_time = descriptor.root_creation_time
        payload: dict[str, object] = {
            "state": "checkpoint",
            "label": label,
            "phase": descriptor.phase.value,
            "execution_id": prepared.execution_id,
            "attempt_id": prepared.attempt_id,
            "control_id": prepared.control_id,
            "job_instance_id": prepared.job_instance_id,
            "container_sha256": hashlib.sha256(descriptor_bytes).hexdigest(),
            "controller_pid": descriptor.controller_pid,
            "controller_creation_time": descriptor.controller_creation_time,
            "root_pid": root_pid,
            "root_creation_time": root_creation_time,
        }
        write_durable_json(
            parent=self._root,
            filename=self.checkpoint_name,
            payload=payload,
            replace=True,
            max_bytes=_CHECKPOINT_MAX_BYTES,
        )
        _sleep_until_terminated()

    def checkpoint_planned(self) -> None:
        self._checkpoint(label="phase:planned")

    def _advance_launch_descriptor(self, prepared: PreparedMaterialAttempt, **kwargs: object) -> str:
        result = super()._advance_launch_descriptor(prepared, **kwargs)  # type: ignore[arg-type]
        phase = kwargs.get("phase")
        phase_value = phase.value if isinstance(phase, MaterialLaunchPhase) else str(phase)
        if self.target_phase == phase_value:
            self._checkpoint(label=f"phase:{phase_value}")
        return result

    def install_raw_boundary_hooks(self) -> None:
        """Install test-only hooks immediately after the native return.

        These hooks deliberately sit below ``_WindowsRunnerOps``.  The real
        callable is invoked first; only then is the checkpoint published and
        the controller held alive.  The parent test terminates this process,
        so no Python cleanup path can turn the checkpoint into a false
        successful hand-off.
        """

        boundary = self.boundary
        if boundary == "raw_create":
            native_api = atomic_subject._NATIVE_API
            if native_api is None:
                raise RuntimeError("raw CreateProcessW hook requires Windows native API")
            original = native_api._create_process

            def create_process_after_return(*args: object) -> object:
                result = original(*args)
                if result:
                    process_information = ctypes.cast(
                        args[-1],
                        ctypes.POINTER(atomic_subject._PROCESS_INFORMATION),
                    ).contents
                    root_pid = int(process_information.dwProcessId)
                    queried_pid, creation_time = runner_subject._query_process_identity(
                        root_pid
                    )
                    if queried_pid != root_pid:
                        raise RuntimeError("raw CreateProcessW hook observed a PID mismatch")
                    self.live_root_pid = root_pid
                    self._checkpoint(
                        label="boundary:raw_create_return",
                        root_pid=root_pid,
                        root_creation_time=creation_time,
                    )
                return result

            native_api._create_process = create_process_after_return
            return

        if boundary == "raw_query":
            if job_subject.win32job is None:
                raise RuntimeError("raw Job Query hook requires pywin32")
            original = job_subject.win32job.QueryInformationJobObject
            seen = False

            def query_after_return(*args: object, **kwargs: object) -> object:
                nonlocal seen
                result = original(*args, **kwargs)
                info_class = args[1] if len(args) > 1 else kwargs.get("JobObjectInformationClass")
                if (
                    not seen
                    and info_class == job_subject.win32job.JobObjectBasicAccountingInformation
                ):
                    seen = True
                    root_pid = self.raw_query_root_pid
                    if root_pid is None:
                        raise RuntimeError("raw Job Query hook has no root PID")
                    _, creation_time = runner_subject._query_process_identity(root_pid)
                    self._checkpoint(
                        label="boundary:raw_query_return",
                        root_pid=root_pid,
                        root_creation_time=creation_time,
                    )
                return result

            job_subject.win32job.QueryInformationJobObject = query_after_return
            return

        if boundary == "raw_resume":
            native_api = atomic_subject._NATIVE_API
            if native_api is None:
                raise RuntimeError("raw ResumeThread hook requires Windows native API")
            original = native_api._resume_thread

            def resume_thread_after_return(*args: object) -> object:
                result = original(*args)
                root_pid = self.live_root_pid
                if root_pid is None:
                    raise RuntimeError("raw ResumeThread hook has no root PID")
                _, creation_time = runner_subject._query_process_identity(root_pid)
                self._checkpoint(
                    label="boundary:raw_resume_return",
                    root_pid=root_pid,
                    root_creation_time=creation_time,
                )
                return result

            native_api._resume_thread = resume_thread_after_return
            return

        if boundary == "raw_write":
            original = pipe_subject._kernel32.WriteFile

            def write_file_after_return(*args: object) -> object:
                result = original(*args)
                prepared = self.prepared
                if prepared is None:
                    raise RuntimeError("raw WriteFile hook has no prepared attempt")
                record = self._records.get(id(prepared))
                if record is None or record.root_pid is None:
                    raise RuntimeError("raw WriteFile hook has no durable root identity")
                self._checkpoint(
                    label="boundary:raw_write_return",
                    root_pid=record.root_pid,
                    root_creation_time=record.root_creation_time,
                )
                return result

            pipe_subject._kernel32.WriteFile = write_file_after_return
            return

        if boundary is not None and boundary.startswith("raw_"):
            raise RuntimeError(f"unsupported raw crash boundary: {boundary}")


class _ResumeBoundaryProcess:
    def __init__(self, inner: object, checkpoint: Callable[[], None]) -> None:
        self._inner = inner
        self._checkpoint = checkpoint
        self.pid = inner.pid  # type: ignore[attr-defined]

    def resume(self) -> int:
        result = self._inner.resume()  # type: ignore[attr-defined]
        self._checkpoint()
        return result

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


class _CrashOps(runner_subject._WindowsRunnerOps):
    def __init__(self, controller: _CrashRunner, *, ready_worker: bool) -> None:
        self.controller = controller
        self.ready_worker = ready_worker

    def preflight_worker_runtime(
        self,
        *,
        prepared: PreparedMaterialAttempt,
    ) -> _WorkerRuntimePreflight:
        base = super().preflight_worker_runtime(prepared=prepared)
        environment = dict(base.environment)
        if self.ready_worker:
            environment["TASK3B_CRASH_READY_THEN_HANG"] = "1"
        return _WorkerRuntimePreflight(
            application_path=base.application_path,
            worker_bootstrap=base.worker_bootstrap,
            backend_root=base.backend_root,
            site_packages=base.site_packages,
            cwd=base.cwd,
            environment=MappingProxyType(environment),
        )

    def create_suspended_process(
        self,
        *,
        launch_spec: _WorkerLaunchSpec,
        job_owner: object,
    ) -> object:
        process = super().create_suspended_process(
            launch_spec=launch_spec,
            job_owner=job_owner,
        )
        self.controller.live_root_pid = int(process.pid)
        if self.controller.boundary == "create":
            pid, creation_time = runner_subject._query_process_identity(process.pid)
            self.controller._checkpoint(
                label="boundary:create_return",
                root_pid=pid,
                root_creation_time=creation_time,
            )
        if self.controller.boundary == "resume":
            return _ResumeBoundaryProcess(
                process,
                lambda: self.controller._checkpoint(
                    label="boundary:resume_return",
                    root_pid=process.pid,
                    root_creation_time=runner_subject._query_process_identity(
                        process.pid
                    )[1],
                ),
            )
        return process

    def verify_single_root_job(self, *, job_owner: object, root_pid: int) -> None:
        self.controller.raw_query_root_pid = root_pid
        super().verify_single_root_job(job_owner=job_owner, root_pid=root_pid)
        if self.controller.boundary == "query":
            _, creation_time = runner_subject._query_process_identity(root_pid)
            self.controller._checkpoint(
                label="boundary:query_return",
                root_pid=root_pid,
                root_creation_time=creation_time,
            )
        self.controller.raw_query_root_pid = None

    def write_secret_frame(
        self,
        *,
        pipe_owner: object,
        immutable_frame: bytes,
        absolute_deadline: float,
    ) -> None:
        super().write_secret_frame(
            pipe_owner=pipe_owner,
            immutable_frame=immutable_frame,
            absolute_deadline=absolute_deadline,
        )
        if self.controller.boundary == "write":
            record = self.controller._records[id(self.controller.prepared)]  # type: ignore[arg-type]
            self.controller._checkpoint(
                label="boundary:write_return",
                root_pid=record.root_pid,
                root_creation_time=record.root_creation_time,
            )

    def wait_worker_ready(self, **kwargs: object) -> object:
        ready = super().wait_worker_ready(**kwargs)  # type: ignore[arg-type]
        if self.controller.boundary == "ready":
            record = self.controller._records[id(self.controller.prepared)]  # type: ignore[arg-type]
            self.controller._checkpoint(
                label="boundary:ready_parent_flush",
                root_pid=record.root_pid,
                root_creation_time=record.root_creation_time,
            )
        return ready


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--target-phase")
    parser.add_argument(
        "--boundary",
        choices=(
            "create",
            "query",
            "resume",
            "write",
            "ready",
            "raw_create",
            "raw_query",
            "raw_resume",
            "raw_write",
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    checkpoint_name = Path(args.checkpoint).name
    if checkpoint_name != args.checkpoint:
        raise SystemExit("checkpoint must be one ordinary file name")
    root = Path(args.root).resolve(strict=True)
    worker = Path(__file__).with_name("material_operation_hanging_worker.py")
    runner = _CrashRunner(
        root=root,
        target_phase=args.target_phase,
        boundary=args.boundary,
        checkpoint_name=checkpoint_name,
    )
    runner_subject._WORKER_BOOTSTRAP_PATH = worker
    prepared = runner.prepare_attempt(
        execution_id=_EXECUTION_ID,
        operation="pexels",
    )
    runner.prepared = prepared
    if args.target_phase == "planned":
        runner.checkpoint_planned()
        return 91
    runner_subject._WINDOWS_RUNNER_OPS = _CrashOps(
        runner,
        ready_worker=args.target_phase == "ready" or args.boundary == "ready",
    )
    runner.install_raw_boundary_hooks()
    request = MaterialOperationRequest(
        protocol_version=1,
        execution_id=prepared.execution_id,
        attempt_id=prepared.attempt_id,
        operation="pexels",
        payload=StockSearchPayload(query="task 3b crash matrix", per_page=1),
    )
    secrets = MaterialOperationSecrets(
        version=1,
        execution_id=prepared.execution_id,
        attempt_id=prepared.attempt_id,
        operation="pexels",
        payload=PexelsSecrets(api_key=_WORKER_SECRET),
    )
    runner.run(
        prepared,
        request,
        secrets=secrets,
        startup_timeout_seconds=30.0,
        operation_timeout_seconds=30.0,
    )
    return 92


if __name__ == "__main__":
    raise SystemExit(main())
