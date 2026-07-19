from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
C_ALIAS = Path(
    r"C:/Users/陈子彦/Documents/Codex/2026-06-14/"
    r"superpowers-plugin-superpowers-openai-curated-trend"
)
RESEARCH_WORKSPACE = Path(r"C:/Users/陈子彦/Documents/Auto Video Engine")
WINDOWS_LAUNCHERS = (
    "启动全部.cmd",
    "启动前端.cmd",
    "启动后端.cmd",
    "一键启动并打开.cmd",
)


def _assert_ascii_launcher_runs_help(filename: str):
    launcher = ROOT / filename

    content = launcher.read_bytes()
    content.decode("ascii")

    if shutil.which("cmd.exe") is None:
        pytest.skip("cmd.exe is only available on Windows-compatible shells")

    env = os.environ.copy()
    env["TVF_NO_PAUSE"] = "1"
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", str(launcher), "--help"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "usage:" in result.stdout
    assert "not recognized" not in (result.stdout + result.stderr)


def test_jianying_calibration_launcher_is_ascii_and_runs_python_module_help():
    _assert_ascii_launcher_runs_help("run_jianying_calibration.cmd")


def test_jianying_controlled_download_launcher_is_ascii_and_runs_help():
    _assert_ascii_launcher_runs_help("run_jianying_controlled_download.cmd")


def _run_launcher_check(root: Path, filename: str) -> subprocess.CompletedProcess[str]:
    if shutil.which("cmd.exe") is None:
        pytest.skip("cmd.exe is only available on Windows-compatible shells")

    env = os.environ.copy()
    env["TVF_NO_PAUSE"] = "1"
    return subprocess.run(
        ["cmd.exe", "/d", "/c", filename, "--check"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
    )


def _physical_root_from_check(output: str) -> Path:
    prefix = "Physical repository root: "
    lines = [line.strip() for line in output.splitlines() if line.startswith(prefix)]
    assert lines, output
    return Path(lines[-1][len(prefix) :])


def test_all_launcher_check_resolves_c_alias_and_d_root_to_same_physical_repo():
    assert C_ALIAS.exists()

    from_d = _run_launcher_check(ROOT, "启动全部.cmd")
    from_c = _run_launcher_check(C_ALIAS, "启动全部.cmd")

    assert from_d.returncode == 0, from_d.stdout + from_d.stderr
    assert from_c.returncode == 0, from_c.stdout + from_c.stderr
    d_physical_root = _physical_root_from_check(from_d.stdout)
    c_physical_root = _physical_root_from_check(from_c.stdout)
    assert d_physical_root == c_physical_root == ROOT
    assert str(RESEARCH_WORKSPACE) not in from_d.stdout + from_c.stdout


@pytest.mark.parametrize("filename", WINDOWS_LAUNCHERS)
def test_launcher_uses_runtime_identity_before_continuing(filename: str):
    content = (ROOT / filename).read_text(encoding="utf-8")

    assert "app.core.runtime_identity --print-root" in content
    assert 'cd /d "%PROJECT_ROOT%"' in content
    assert str(RESEARCH_WORKSPACE) not in content


def test_launcher_ports_remain_8002_and_5176():
    all_content = (ROOT / "启动全部.cmd").read_text(encoding="utf-8")
    backend_content = (ROOT / "启动后端.cmd").read_text(encoding="utf-8")
    frontend_content = (ROOT / "启动前端.cmd").read_text(encoding="utf-8")
    one_click_content = (ROOT / "一键启动并打开.cmd").read_text(
        encoding="utf-8"
    )

    assert 'set "BACKEND_PORT=8002"' in all_content
    assert 'set "FRONTEND_PORT=5176"' in all_content
    assert 'set "BACKEND_PORT=8002"' in backend_content
    assert 'set "BACKEND_PORT=8002"' in frontend_content
    assert 'set "FRONTEND_PORT=5176"' in frontend_content
    assert 'set "BACKEND_PORT=8002"' in one_click_content
    assert 'set "FRONTEND_PORT=5176"' in one_click_content


@pytest.mark.parametrize(
    ("filename", "server_commands"),
    [
        ("启动全部.cmd", (" -m uvicorn ", "vite.cmd --host")),
        ("启动前端.cmd", ("vite.cmd --host",)),
        ("启动后端.cmd", (" -m uvicorn ",)),
        ("一键启动并打开.cmd", (" -m uvicorn ", "vite.cmd --host")),
    ],
)
def test_launcher_check_guard_precedes_every_server_command(
    filename: str,
    server_commands: tuple[str, ...],
):
    content = (ROOT / filename).read_text(encoding="utf-8")
    check_guard = content.index('if /I "%~1"=="--check"')

    for command in server_commands:
        assert check_guard < content.index(command)


@pytest.mark.parametrize("filename", WINDOWS_LAUNCHERS)
def test_launcher_check_returns_without_starting_services(filename: str):
    result = _run_launcher_check(ROOT, filename)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Starting " not in result.stdout
    assert "uvicorn" not in result.stdout + result.stderr
