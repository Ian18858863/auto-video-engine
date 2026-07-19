"""Worker fixture that completes secret decode and then waits for Job termination."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import sys
import time


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--backend-root", required=True)
    parser.add_argument("--site-packages", required=True)
    parser.add_argument("--attempt-dir", required=True)
    parser.add_argument("--pipe-name", required=True)
    args = parser.parse_args()
    backend_root = Path(args.backend_root).resolve(strict=True)
    site_packages = Path(args.site_packages).resolve(strict=True)
    attempt_directory = Path(args.attempt_dir).resolve(strict=True)
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

        from app.pipeline.material_operation_protocol import (
            MAX_SECRET_BYTES,
            decode_material_operation_request,
            decode_material_operation_secrets,
        )
        from app.core.windows_durable_file import write_durable_bytes
        from app.core.windows_storage_policy import approve_physical_d_write_root
        from app.pipeline.material_operation_control_protocol import (
            MAX_READY_BYTES,
            MaterialWorkerReady,
            encode_material_worker_ready,
        )

        request = decode_material_operation_request(
            (attempt_directory / "request.json").read_bytes()
        )
        pipe_handle = win32file.CreateFile(
            args.pipe_name,
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
                raise RuntimeError("secret read did not complete")
        finally:
            pipe_handle.Close()
        frame = bytes(frame)
        payload_length = int.from_bytes(frame[:4], "big")
        if payload_length != len(frame) - 4:
            raise RuntimeError("secret frame length mismatch")
        decode_material_operation_secrets(frame[4:], request=request)
        if os.environ.get("TASK3B_CRASH_READY_THEN_HANG") == "1":
            ready = MaterialWorkerReady(
                protocol_version=1,
                state="ready",
                execution_id=request.execution_id,
                attempt_id=request.attempt_id,
                operation=request.operation,
                root_pid=os.getpid(),
                request_sha256=hashlib.sha256(
                    (attempt_directory / "request.json").read_bytes()
                ).hexdigest(),
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
            finally:
                approved_attempt.close()
            time.sleep(2.0)
            late_attempt = approve_physical_d_write_root(attempt_directory)
            try:
                write_durable_bytes(
                    parent=late_attempt,
                    filename="late-write.txt",
                    payload=b"worker survived controller crash",
                    replace=False,
                    max_bytes=64 * 1024,
                )
            finally:
                late_attempt.close()
        time.sleep(60.0)
        return 91
    finally:
        dll_directory.close()


if __name__ == "__main__":
    raise SystemExit(main())
