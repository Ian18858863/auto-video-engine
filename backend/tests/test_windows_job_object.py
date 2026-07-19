from __future__ import annotations

from pathlib import Path
import subprocess
import sys
from unittest import mock

import pytest
import win32api
import win32con
import win32event
import win32job
import win32process

from app.pipeline import windows_job_object as subject


def _assign_test_process_to_job_or_terminate(
    job_handle: object,
    process_handle: object,
) -> None:
    try:
        win32job.AssignProcessToJobObject(job_handle, process_handle)
    except BaseException:
        win32process.TerminateProcess(process_handle, 0x7001)
        if (
            win32event.WaitForSingleObject(process_handle, 5_000)
            != win32event.WAIT_OBJECT_0
        ):
            raise AssertionError("test process did not exit after Assign failure")
        raise


def _terminate_test_process_if_running(process_handle: object) -> None:
    if (
        win32event.WaitForSingleObject(process_handle, 0)
        == win32event.WAIT_OBJECT_0
    ):
        return
    win32process.TerminateProcess(process_handle, 0x7002)
    if (
        win32event.WaitForSingleObject(process_handle, 5_000)
        != win32event.WAIT_OBJECT_0
    ):
        raise AssertionError("test process did not exit during fixture cleanup")


def test_create_configures_unnamed_noninheritable_kill_on_close_job() -> None:
    job = subject.WindowsJobObject.create_unnamed_kill_on_close()
    try:
        info = win32job.QueryInformationJobObject(
            job.handle,
            win32job.JobObjectExtendedLimitInformation,
        )
        flags = int(info["BasicLimitInformation"]["LimitFlags"])

        assert flags & win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        assert not flags & win32job.JOB_OBJECT_LIMIT_BREAKAWAY_OK
        assert not flags & win32job.JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK
        assert not (
            win32api.GetHandleInformation(job.handle) & win32con.HANDLE_FLAG_INHERIT
        )
        assert job.active_process_count() == 0
        assert job.process_ids() == ()
        assert job.wait_until_empty(0.0) is True
    finally:
        job.close()

    job.close()
    with pytest.raises(RuntimeError, match="closed"):
        _ = job.handle


def test_configuration_failure_closes_created_job_handle_exactly_once() -> None:
    handle = mock.Mock()

    with (
        mock.patch.object(subject.win32job, "CreateJobObject", return_value=handle),
        mock.patch.object(
            subject.win32api,
            "SetHandleInformation",
            side_effect=OSError("cannot clear inherit flag"),
        ),
    ):
        with pytest.raises(OSError, match="cannot clear inherit flag"):
            subject.WindowsJobObject.create_unnamed_kill_on_close()

    handle.Close.assert_called_once_with()


def test_real_job_fixture_terminates_and_waits_when_assign_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, int]] = []
    monkeypatch.setattr(
        win32job,
        "AssignProcessToJobObject",
        mock.Mock(side_effect=OSError("assign failed")),
    )
    monkeypatch.setattr(
        win32process,
        "TerminateProcess",
        lambda _handle, code: events.append(("terminate", code)),
    )
    monkeypatch.setattr(
        win32event,
        "WaitForSingleObject",
        lambda _handle, timeout: events.append(("wait", timeout))
        or win32event.WAIT_OBJECT_0,
    )

    with pytest.raises(OSError, match="assign failed"):
        _assign_test_process_to_job_or_terminate(11, 22)

    assert events == [("terminate", 0x7001), ("wait", 5_000)]


def test_query_terminate_and_wait_cover_a_real_job_process() -> None:
    job = subject.WindowsJobObject.create_unnamed_kill_on_close()
    process_handle = None
    thread_handle = None
    try:
        application = Path(sys.executable).resolve()
        command_line = subprocess.list2cmdline(
            [str(application), "-I", "-S", "-c", "import time; time.sleep(60)"]
        )
        startup_info = win32process.STARTUPINFO()
        process_handle, thread_handle, pid, _tid = win32process.CreateProcess(
            str(application),
            command_line,
            None,
            None,
            False,
            win32con.CREATE_SUSPENDED | win32con.CREATE_NO_WINDOW,
            None,
            str(Path(__file__).resolve().parents[2]),
            startup_info,
        )
        _assign_test_process_to_job_or_terminate(job.handle, process_handle)
        assert win32process.ResumeThread(thread_handle) == 1

        assert job.active_process_count() == 1
        assert job.process_ids() == (pid,)
        assert job.wait_until_empty(0.0) is False

        job.terminate(0xE0000001)

        assert job.wait_until_empty(5.0) is True
        assert job.active_process_count() == 0
        assert job.process_ids() == ()
        assert win32event.WaitForSingleObject(process_handle, 5_000) == win32event.WAIT_OBJECT_0
    finally:
        try:
            if process_handle is not None:
                _terminate_test_process_if_running(process_handle)
        finally:
            if thread_handle is not None:
                thread_handle.Close()
            if process_handle is not None:
                process_handle.Close()
            job.close()
