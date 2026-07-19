"""Minimal real Windows worker used only by the Task 3B containment tests."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import sys


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--backend-root", required=True)
    parser.add_argument("--site-packages", required=True)
    parser.add_argument("--attempt-dir", required=True)
    parser.add_argument("--pipe-name", required=True)
    return parser.parse_args()


def main() -> int:
    arguments = _arguments()
    backend_root = Path(arguments.backend_root).resolve(strict=True)
    site_packages = Path(arguments.site_packages).resolve(strict=True)
    attempt_directory = Path(arguments.attempt_dir).resolve(strict=True)
    for path in (
        site_packages / "Pythonwin",
        site_packages / "win32" / "lib",
        site_packages / "win32",
        site_packages,
        backend_root,
    ):
        sys.path.insert(0, str(path))
    dll_directory = os.add_dll_directory(str(site_packages / "pywin32_system32"))
    try:
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
            MaterialWorkerResult,
            decode_material_operation_request,
            decode_material_operation_secrets,
            encode_material_worker_result,
        )

        request_bytes = (attempt_directory / "request.json").read_bytes()
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
            status, frame = win32file.ReadFile(pipe_handle, 4 + MAX_SECRET_BYTES)
            if status not in {0, None}:
                raise RuntimeError("secret pipe read did not complete synchronously")
        finally:
            pipe_handle.Close()
        frame = bytes(frame)
        if not 5 <= len(frame) <= 4 + MAX_SECRET_BYTES:
            raise RuntimeError("secret frame length is outside the protocol bounds")
        payload_length = int.from_bytes(frame[:4], "big")
        if payload_length != len(frame) - 4:
            raise RuntimeError("secret frame prefix does not match its payload")
        decode_material_operation_secrets(frame[4:], request=request)

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
        result = MaterialWorkerResult(
            protocol_version=1,
            execution_id=request.execution_id,
            attempt_id=request.attempt_id,
            operation=request.operation,
            status="failed",
            selected_artifact="",
            artifacts=(),
            reason_code="fixture_completed",
            reason="Task 3B real worker fixture completed without a provider call.",
        )
        approved_attempt = approve_physical_d_write_root(attempt_directory)
        try:
            write_durable_bytes(
                parent=approved_attempt,
                filename="ready.json",
                payload=encode_material_worker_ready(ready),
                replace=False,
                max_bytes=MAX_READY_BYTES,
            )
            write_durable_bytes(
                parent=approved_attempt,
                filename="result.json",
                payload=encode_material_worker_result(result),
                replace=False,
                max_bytes=MAX_RESULT_BYTES,
            )
        finally:
            approved_attempt.close()
        return 23
    finally:
        dll_directory.close()


if __name__ == "__main__":
    raise SystemExit(main())
