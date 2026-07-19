from __future__ import annotations

import ast
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from app.materials.stock_rights_evidence import build_stock_rights_evidence

from app.core.windows_storage_policy import approve_physical_d_write_root
from app.pipeline.material_operation_protocol import (
    CoverrSecrets,
    InfoMotionPayload,
    InfoMotionSecrets,
    MaterialOperationRequest,
    MaterialOperationSecrets,
    OfficialCapturePayload,
    OfficialCaptureSecrets,
    PexelsSecrets,
    PixabaySecrets,
    StockSearchPayload,
)


EXECUTION_ID = "123e4567-e89b-12d3-a456-426614174000"
PEXELS_STOCK_SECRET = "stock-pexels-secret-canary"
PIXABAY_STOCK_SECRET = "stock-pixabay-secret-canary"


def _attempt(tmp_path: Path):
    attempt = tmp_path / "attempt"
    attempt.mkdir(parents=True)
    (attempt / "payload").mkdir()
    return attempt, approve_physical_d_write_root(attempt)


def _request(operation: str) -> MaterialOperationRequest:
    payload: Any
    if operation == "official_capture":
        payload = OfficialCapturePayload(
            source_url="https://example.com/article",
            source_name="Example",
            page_title="A page",
        )
    elif operation in {"pexels", "pixabay", "coverr", "stock_candidates"}:
        payload = StockSearchPayload(query="office", per_page=2)
    else:
        payload = InfoMotionPayload(project_name="project", title="title")
    return MaterialOperationRequest(
        protocol_version=2 if operation == "stock_candidates" else 1,
        execution_id=EXECUTION_ID,
        attempt_id=str(uuid4()),
        operation=operation,  # type: ignore[arg-type]
        payload=payload,
    )


def _secrets(request: MaterialOperationRequest) -> MaterialOperationSecrets:
    payload: Any
    if request.operation == "official_capture":
        payload = OfficialCaptureSecrets()
    elif request.operation == "pexels":
        payload = PexelsSecrets(api_key="pexels-injected-secret")
    elif request.operation == "pixabay":
        payload = PixabaySecrets(api_key="pixabay-injected-secret")
    elif request.operation == "stock_candidates":
        from app.pipeline import material_operation_protocol as protocol

        payload = protocol.StockCandidateSecrets(
            pexels_api_key=PEXELS_STOCK_SECRET,
            pixabay_api_key=PIXABAY_STOCK_SECRET,
        )
    elif request.operation == "coverr":
        payload = CoverrSecrets()
    else:
        payload = InfoMotionSecrets()
    return MaterialOperationSecrets(
        version=request.protocol_version,
        execution_id=request.execution_id,
        attempt_id=request.attempt_id,
        operation=request.operation,
        payload=payload,
    )


def _fake_adapter(name: str, calls: list[tuple[str, dict[str, Any]]]):
    def run(*, request, secrets, paths):
        output = paths.root / f"{name}.mp4"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(f"{name}-bytes".encode())
        calls.append(
            (
                name,
                {
                    "request": request,
                    "secrets": secrets,
                    "paths": paths,
                },
            )
        )
        return {
            "success": True,
            "artifacts": [
                {
                    "path": output,
                    "media_kind": "video",
                    "source_type": name,
                    "source_url": "https://example.com/source",
                    "evidence_kind": "atmosphere",
                    "is_locally_generated": False,
                }
            ],
            "selected_artifact": output,
        }

    return run


def _stock_adapter(
    provider: str,
    items: tuple[tuple[str, str, bytes], ...],
    calls: list[tuple[str, str, Path]],
    *,
    include_rights: bool = True,
):
    def run(*, request, secrets, paths):
        calls.append((request.operation, secrets.payload.api_key, paths.root))
        output_dir = paths.root / provider.lower()
        output_dir.mkdir(parents=True, exist_ok=True)
        artifacts: list[dict[str, object]] = []
        for index, (filename, media_kind, content) in enumerate(items, start=1):
            output = output_dir / filename
            output.write_bytes(content)
            source_url = (
                f"https://www.pexels.com/video/example-{index}/"
                if provider == "Pexels"
                else f"https://pixabay.com/videos/id-{index}/"
            )
            rights_evidence = None
            if include_rights:
                rights_evidence = build_stock_rights_evidence(
                    provider=provider,
                    provider_asset_id=str(index),
                    media_type="photo" if media_kind == "image" else "video",
                    asset_page_url=source_url,
                    contributor_name=f"{provider} contributor {index}",
                    search_query="office",
                    observed_at="2026-07-18T00:00:00+00:00",
                )
            artifacts.append(
                {
                    "path": output,
                    "media_kind": media_kind,
                    "source_type": provider,
                    "source_url": source_url,
                    "evidence_kind": "atmosphere",
                    "is_locally_generated": False,
                    "rights_evidence": rights_evidence,
                }
            )
        return {
            "success": True,
            "artifacts": artifacts,
            "selected_artifact": artifacts[0]["path"] if artifacts else None,
            "reason": f"{provider} completed",
        }

    return run


def test_worker_dispatches_exactly_one_staging_adapter_per_operation(tmp_path: Path):
    from app.pipeline.material_operation_worker import MaterialWorkerServices, execute_material_operation

    calls: list[tuple[str, dict[str, Any]]] = []
    services = MaterialWorkerServices(
        official_capture=_fake_adapter("official_capture", calls),
        pexels=_fake_adapter("pexels", calls),
        pixabay=_fake_adapter("pixabay", calls),
        coverr=_fake_adapter("coverr", calls),
        info_motion=_fake_adapter("info_motion", calls),
    )
    attempt_paths: list[Path] = []
    for index, operation in enumerate(("official_capture", "pexels", "pixabay", "coverr", "info_motion")):
        attempt_path, attempt = _attempt(tmp_path / f"attempt-{index}")
        attempt_paths.append(attempt_path)
        try:
            request = _request(operation)
            result = execute_material_operation(request, _secrets(request), attempt, services=services)
            assert result.status == "succeeded"
            assert len(result.artifacts) == 1
            assert result.selected_artifact.startswith("payload/")
            artifact = result.artifacts[0]
            artifact_path = attempt_path / Path(artifact.relative_path)
            assert artifact_path.is_file()
            assert artifact.sha256 == hashlib.sha256(artifact_path.read_bytes()).hexdigest()
            assert artifact.size_bytes == artifact_path.stat().st_size
        finally:
            attempt.close()
    assert [name for name, _ in calls] == [
        "official_capture",
        "pexels",
        "pixabay",
        "coverr",
        "info_motion",
    ]
    assert all(kwargs["paths"].root.name == "payload" for _, kwargs in calls)
    assert all(not (path / "container.json").exists() for path in attempt_paths)


def test_worker_passes_explicit_keys_and_never_reads_key_files(tmp_path: Path, monkeypatch):
    from app.pipeline.material_operation_worker import MaterialWorkerServices, execute_material_operation

    seen: list[tuple[str, str]] = []

    def pexels(*, request, secrets, paths):
        seen.append((request.operation, secrets.payload.api_key))
        output = paths.root / "pexels.mp4"
        output.write_bytes(b"pexels")
        return {"success": True, "artifacts": [{"path": output, "media_kind": "video", "source_type": "Pexels", "evidence_kind": "atmosphere"}]}

    def pixabay(*, request, secrets, paths):
        seen.append((request.operation, secrets.payload.api_key))
        output = paths.root / "pixabay.mp4"
        output.write_bytes(b"pixabay")
        return {"success": True, "artifacts": [{"path": output, "media_kind": "video", "source_type": "Pixabay", "evidence_kind": "atmosphere"}]}

    services = MaterialWorkerServices(pexels=pexels, pixabay=pixabay)
    for index, operation in enumerate(("pexels", "pixabay")):
        _attempt_path, attempt = _attempt(tmp_path / f"attempt-{index}")
        try:
            request = _request(operation)
            result = execute_material_operation(request, _secrets(request), attempt, services=services)
            assert result.status == "succeeded"
        finally:
            attempt.close()
    assert seen == [
        ("pexels", "pexels-injected-secret"),
        ("pixabay", "pixabay-injected-secret"),
    ]


def test_stock_candidates_calls_both_existing_adapters_in_one_attempt_and_does_not_select(
    tmp_path: Path,
) -> None:
    from app.pipeline.material_operation_worker import (
        MaterialWorkerServices,
        execute_material_operation,
    )

    attempt_path, attempt = _attempt(tmp_path)
    request = _request("stock_candidates")
    calls: list[tuple[str, str, Path]] = []
    services = MaterialWorkerServices(
        pexels=_stock_adapter(
            "Pexels",
            (
                ("candidate-video.mp4", "video", b"pexels-video"),
                ("candidate-image.jpg", "image", b"pexels-image"),
            ),
            calls,
        ),
        pixabay=_stock_adapter(
            "Pixabay",
            (("candidate-best.mp4", "video", b"pixabay-best"),),
            calls,
        ),
    )

    try:
        result = execute_material_operation(
            request,
            _secrets(request),
            attempt,
            services=services,
        )

        assert result.status == "succeeded"
        assert result.protocol_version == 2
        assert result.operation == "stock_candidates"
        assert result.selected_artifact == ""
        assert len(result.artifacts) == 3
        assert {item.source_type for item in result.artifacts} == {"Pexels", "Pixabay"}
        assert calls == [
            ("pexels", PEXELS_STOCK_SECRET, attempt_path / "payload"),
            ("pixabay", PIXABAY_STOCK_SECRET, attempt_path / "payload"),
        ]
        result_files = {
            (attempt_path / Path(item.relative_path)).resolve()
            for item in result.artifacts
        }
        payload_files = {
            path.resolve()
            for path in (attempt_path / "payload").rglob("*")
            if path.is_file()
        }
        assert payload_files == result_files
    finally:
        attempt.close()


def test_stock_candidates_deduplicates_content_and_fails_below_three(
    tmp_path: Path,
) -> None:
    from app.pipeline.material_operation_worker import (
        MaterialWorkerServices,
        execute_material_operation,
    )

    attempt_path, attempt = _attempt(tmp_path)
    request = _request("stock_candidates")
    calls: list[tuple[str, str, Path]] = []
    services = MaterialWorkerServices(
        pexels=_stock_adapter(
            "Pexels",
            (
                ("candidate-video.mp4", "video", b"duplicate-content"),
                ("candidate-image.jpg", "image", b"pexels-image"),
            ),
            calls,
        ),
        pixabay=_stock_adapter(
            "Pixabay",
            (("candidate-best.mp4", "video", b"duplicate-content"),),
            calls,
        ),
    )

    try:
        result = execute_material_operation(
            request,
            _secrets(request),
            attempt,
            services=services,
        )

        assert [item[0] for item in calls] == ["pexels", "pixabay"]
        assert result.status == "failed"
        assert result.protocol_version == 2
        assert result.selected_artifact == ""
        assert result.artifacts == ()
        assert result.reason_code == "candidate_batch_invalid"
        assert len(
            [path for path in (attempt_path / "payload").rglob("*") if path.is_file()]
        ) == 2
    finally:
        attempt.close()


def test_stock_candidates_rejects_placeholders_instead_of_filling_the_batch(
    tmp_path: Path,
) -> None:
    from app.pipeline.material_operation_worker import (
        MaterialWorkerServices,
        execute_material_operation,
    )

    attempt_path, attempt = _attempt(tmp_path)
    request = _request("stock_candidates")
    calls: list[tuple[str, str, Path]] = []
    pexels = _stock_adapter(
        "Pexels",
        (
            ("candidate-video.mp4", "video", b"pexels-video"),
            ("candidate-image.jpg", "image", b"pexels-image"),
        ),
        calls,
    )

    def pixabay_placeholder(*, request, secrets, paths):
        calls.append((request.operation, secrets.payload.api_key, paths.root))
        output = paths.root / "pixabay-placeholder.mp4"
        output.write_bytes(b"placeholder")
        return {
            "success": True,
            "artifacts": [
                {
                    "path": output,
                    "media_kind": "video",
                    "source_type": "Pixabay",
                    "source_url": "",
                    "evidence_kind": "explanation",
                    "is_locally_generated": True,
                }
            ],
            "selected_artifact": output,
        }

    try:
        result = execute_material_operation(
            request,
            _secrets(request),
            attempt,
            services=MaterialWorkerServices(
                pexels=pexels,
                pixabay=pixabay_placeholder,
            ),
        )

        assert result.status == "failed"
        assert result.artifacts == ()
        assert result.selected_artifact == ""
        assert result.reason_code == "candidate_batch_invalid"
    finally:
        attempt.close()


def test_worker_builtin_pexels_uses_ephemeral_fingerprint_registry(tmp_path: Path, monkeypatch):
    import app.materials.pexels as pexels_module
    import app.pipeline.material_operation_worker as worker
    from app.materials.stock_rights_evidence import build_stock_rights_evidence

    attempt_path, attempt = _attempt(tmp_path)
    request = _request("pexels")
    captured: dict[str, Any] = {}

    class FakePexelsService:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def download_one_video_and_one_photo(self, query, *, per_page):
            output = captured["paths"].root / "provider.mp4"
            output.write_bytes(b"provider")
            rights_evidence = build_stock_rights_evidence(
                provider="Pexels",
                provider_asset_id="101",
                media_type="video",
                asset_page_url="https://www.pexels.com/video/provider-101/",
                contributor_name="Provider Author",
                search_query=query,
                observed_at="2026-07-18T09:30:00+00:00",
            )
            item = SimpleNamespace(
                status="已下载",
                save_path=str(output),
                asset_type="video",
                source_url="https://www.pexels.com/video/provider-101/",
                rights_evidence=rights_evidence,
                reason="",
            )
            empty = SimpleNamespace(
                status="失败",
                save_path="",
                asset_type="photo",
                source_url="",
                reason="",
            )
            return SimpleNamespace(video=item, photo=empty)

    monkeypatch.setattr(pexels_module, "PexelsDownloadService", FakePexelsService)
    try:
        # The built-in adapter receives the same immutable path object as a
        # real provider; the worker must not create the canonical registry.
        captured["paths"] = SimpleNamespace(root=attempt_path / "payload")
        result = worker.execute_material_operation(
            request,
            _secrets(request),
            attempt,
            services=worker.MaterialWorkerServices(pexels=worker._default_pexels),
        )
        assert result.status == "succeeded"
        assert result.artifacts[0].rights_evidence is not None
        assert result.artifacts[0].rights_evidence["provider_asset_id"] == "101"
        assert captured["fingerprint_registry"].__class__.__name__ == "_ContainedFingerprintRegistry"
        from app.materials.asset_paths import AutoMaterialPaths

        assert not AutoMaterialPaths(root=attempt_path / "payload").fingerprint_registry_path.exists()
    finally:
        attempt.close()


def test_worker_rejects_unknown_operation_before_any_write(tmp_path: Path):
    from app.pipeline.material_operation_worker import MaterialWorkerServices, execute_material_operation
    from app.pipeline.material_operation_protocol import MaterialProtocolError

    attempt_path, attempt = _attempt(tmp_path)
    request = _request("coverr")
    secrets = _secrets(request)
    object.__setattr__(request, "operation", "unknown")
    try:
        with pytest.raises((ValueError, MaterialProtocolError)):
            execute_material_operation(request, secrets, attempt, services=MaterialWorkerServices())
        assert sorted(path.name for path in attempt_path.iterdir()) == ["payload"]
    finally:
        attempt.close()


def test_worker_artifacts_are_hashed_and_cannot_escape_payload(tmp_path: Path):
    from app.pipeline.material_operation_worker import MaterialWorkerServices, execute_material_operation

    attempt_path, attempt = _attempt(tmp_path)
    request = _request("coverr")
    outside = attempt_path.parent / "outside.mp4"
    outside.write_bytes(b"outside")

    def escaping(*, request, secrets, paths):
        return {"success": True, "artifacts": [{"path": outside, "media_kind": "video", "source_type": "Coverr", "evidence_kind": "atmosphere"}]}

    try:
        with pytest.raises(ValueError, match="payload"):
            execute_material_operation(
                request,
                _secrets(request),
                attempt,
                services=MaterialWorkerServices(coverr=escaping),
            )
    finally:
        attempt.close()


def test_worker_paths_are_immutable_for_adapters(tmp_path: Path):
    from app.pipeline.material_operation_worker import MaterialWorkerServices, execute_material_operation

    attempt_path, attempt = _attempt(tmp_path)
    request = _request("coverr")
    outside = tmp_path / "outside-mutated"

    def mutate_paths(*, request, secrets, paths):
        paths.root = outside
        outside.mkdir(parents=True, exist_ok=True)
        (outside / "escaped.mp4").write_bytes(b"escaped")
        return {"success": True, "artifacts": []}

    try:
        result = execute_material_operation(
            request,
            _secrets(request),
            attempt,
            services=MaterialWorkerServices(coverr=mutate_paths),
        )
        assert result.status == "failed"
        assert not outside.exists()
        assert not any(path.name == "escaped.mp4" for path in tmp_path.rglob("*"))
    finally:
        attempt.close()


def test_worker_rejects_nonfresh_payload_before_adapter_call(tmp_path: Path):
    from app.pipeline.material_operation_worker import MaterialWorkerServices, execute_material_operation

    attempt_path, attempt = _attempt(tmp_path)
    stale = attempt_path / "payload" / "stale.mp4"
    stale.write_bytes(b"stale")
    called = False
    request = _request("coverr")

    def adapter(*, request, secrets, paths):
        nonlocal called
        called = True
        return {"success": True, "artifacts": []}

    try:
        with pytest.raises(ValueError, match="must be empty"):
            execute_material_operation(
                request,
                _secrets(request),
                attempt,
                services=MaterialWorkerServices(coverr=adapter),
            )
        assert called is False
    finally:
        attempt.close()


def test_worker_rejects_payload_hardlinks(tmp_path: Path):
    from app.pipeline.material_operation_worker import MaterialWorkerServices, execute_material_operation

    attempt_path, attempt = _attempt(tmp_path)
    payload = attempt_path / "payload"
    request = _request("coverr")
    secrets = _secrets(request)
    outside = tmp_path / "outside-source.mp4"
    outside.write_bytes(b"outside")
    hardlink = payload / "hardlink.mp4"
    os.link(outside, hardlink)

    def hardlink_adapter(*, request, secrets, paths):
        return {
            "success": True,
            "artifacts": [
                {
                    "path": hardlink,
                    "media_kind": "video",
                    "source_type": "Coverr",
                    "source_url": "https://example.com/source",
                    "evidence_kind": "atmosphere",
                }
            ],
        }

    try:
        with pytest.raises(ValueError, match="single-link"):
            execute_material_operation(
                request,
                secrets,
                attempt,
                services=MaterialWorkerServices(coverr=hardlink_adapter),
            )

    finally:
        attempt.close()


def test_worker_rejects_payload_symlinks(tmp_path: Path):
    from app.pipeline.material_operation_worker import MaterialWorkerServices, execute_material_operation

    attempt_path, attempt = _attempt(tmp_path)
    payload = attempt_path / "payload"
    request = _request("coverr")
    secrets = _secrets(request)
    target = payload / "target.mp4"
    target.write_bytes(b"target")
    link = payload / "link.mp4"
    try:
        link.symlink_to(target)
    except OSError:
        attempt.close()
        pytest.skip("this Windows account cannot create a symlink")

    def symlink_adapter(*, request, secrets, paths):
        return {
            "success": True,
            "artifacts": [
                {
                    "path": link,
                    "media_kind": "video",
                    "source_type": "Coverr",
                    "source_url": "https://example.com/source",
                    "evidence_kind": "atmosphere",
                }
            ],
        }

    try:
        with pytest.raises(ValueError, match="reparse"):
            execute_material_operation(
                request,
                secrets,
                attempt,
                services=MaterialWorkerServices(coverr=symlink_adapter),
            )
    finally:
        attempt.close()


def test_worker_source_does_not_import_parent_framework_or_use_escape_processes():
    source = Path(__file__).parents[1] / "app" / "pipeline" / "material_operation_worker.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imports.update(
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    )
    text = source.read_text(encoding="utf-8")
    assert not any(name.startswith(prefix) for name in imports for prefix in ("fastapi", "sqlalchemy"))
    assert "ProductionPipeline" not in text
    assert "Win32_Process.Create" not in text
    assert "daemonize" not in text.lower()
    assert "breakaway" not in text.lower()
    execute = next(
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "execute_material_operation"
    )
    match_nodes = [node for node in ast.walk(execute) if isinstance(node, ast.Match)]
    assert len(match_nodes) == 1
    literal_cases = {
        pattern.value.value
        for case in match_nodes[0].cases
        for pattern in [case.pattern]
        if isinstance(pattern, ast.MatchValue)
        and isinstance(pattern.value, ast.Constant)
    }
    assert literal_cases == {
        "official_capture",
        "pexels",
        "pixabay",
        "coverr",
        "info_motion",
        "stock_candidates",
    }
    assert any(isinstance(case.pattern, ast.MatchAs) for case in match_nodes[0].cases)


def test_worker_secret_frame_rejects_trailing_bytes(monkeypatch):
    import win32file
    from app.pipeline.material_operation_worker import _reject_trailing_secret_bytes

    monkeypatch.setattr(win32file, "ReadFile", lambda _handle, _size: (0, b"x"))
    with pytest.raises(RuntimeError, match="trailing"):
        _reject_trailing_secret_bytes(object(), win32file=win32file)


def test_worker_secret_frame_accepts_broken_pipe_eof(monkeypatch):
    import win32file
    from app.pipeline.material_operation_worker import _reject_trailing_secret_bytes

    monkeypatch.setattr(win32file, "ReadFile", lambda _handle, _size: (109, b""))
    _reject_trailing_secret_bytes(object(), win32file=win32file)


def test_info_motion_frames_and_mp4_stay_inside_payload(tmp_path: Path, monkeypatch):
    import app.pipeline.material_operation_worker as worker

    attempt_path, attempt = _attempt(tmp_path)
    observed: dict[str, Path] = {}

    def fake_video(pattern, output_path, *, duration_seconds=6, fps=12):
        observed["pattern"] = pattern
        observed["output"] = output_path
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"motion-video")

    monkeypatch.setattr(worker, "_frames_to_video", fake_video)
    request = _request("info_motion")
    try:
        result = worker.execute_material_operation(request, _secrets(request), attempt)
        assert result.status == "succeeded"
        payload = attempt_path / "payload"
        assert observed["pattern"].resolve().is_relative_to(payload.resolve())
        assert observed["output"].resolve().is_relative_to(payload.resolve())
        frames = list(observed["pattern"].parent.glob("motion_*.png"))
        assert len(frames) == 12
        assert all(path.resolve().is_relative_to(payload.resolve()) for path in frames)
    finally:
        attempt.close()


def test_worker_failure_redacts_secret_and_url_query_values(tmp_path: Path):
    from app.pipeline.material_operation_worker import MaterialWorkerServices, execute_material_operation

    attempt_path, attempt = _attempt(tmp_path)
    request = _request("pexels")
    secrets = _secrets(request)
    secret = secrets.payload.api_key

    def failing(*, request, secrets, paths):
        raise RuntimeError(
            f"failed https://example.com/search?api_key={secret}&token=visible {secret}"
        )

    try:
        result = execute_material_operation(
            request,
            secrets,
            attempt,
            services=MaterialWorkerServices(pexels=failing),
        )
        assert result.status == "failed"
        assert secret not in result.reason
        assert "token=visible" not in result.reason
        for path in attempt_path.rglob("*"):
            if path.is_file():
                assert secret.encode() not in path.read_bytes()
    finally:
        attempt.close()


def test_stock_candidates_failure_and_result_bytes_redact_both_provider_keys(
    tmp_path: Path,
) -> None:
    from app.pipeline.material_operation_protocol import encode_material_worker_result
    from app.pipeline.material_operation_worker import (
        MaterialWorkerServices,
        execute_material_operation,
    )

    attempt_path, attempt = _attempt(tmp_path)
    request = _request("stock_candidates")
    calls: list[tuple[str, str, Path]] = []
    pexels = _stock_adapter(
        "Pexels",
        (
            ("candidate-video.mp4", "video", b"pexels-video"),
            ("candidate-image.jpg", "image", b"pexels-image"),
        ),
        calls,
    )

    def pixabay_failure(*, request, secrets, paths):
        raise RuntimeError(
            f"provider failed {PEXELS_STOCK_SECRET} {PIXABAY_STOCK_SECRET} "
            f"https://example.com/search?key={PIXABAY_STOCK_SECRET}"
        )

    try:
        result = execute_material_operation(
            request,
            _secrets(request),
            attempt,
            services=MaterialWorkerServices(
                pexels=pexels,
                pixabay=pixabay_failure,
            ),
        )
        encoded = encode_material_worker_result(result)

        assert result.status == "failed"
        assert result.protocol_version == 2
        for secret in (PEXELS_STOCK_SECRET, PIXABAY_STOCK_SECRET):
            assert secret not in result.reason
            assert secret.encode() not in encoded
            for path in attempt_path.rglob("*"):
                assert secret not in path.name
                if path.is_file():
                    assert secret.encode() not in path.read_bytes()
    finally:
        attempt.close()


def test_stock_candidates_scrubs_both_keys_from_names_and_file_bytes(
    tmp_path: Path,
) -> None:
    from app.pipeline.material_operation_protocol import encode_material_worker_result
    from app.pipeline.material_operation_worker import (
        MaterialWorkerServices,
        execute_material_operation,
    )

    attempt_path, attempt = _attempt(tmp_path)
    request = _request("stock_candidates")
    calls: list[tuple[str, str, Path]] = []
    pexels = _stock_adapter(
        "Pexels",
        (
            ("candidate-video.mp4", "video", b"pexels-video"),
            ("candidate-image.jpg", "image", b"pexels-image"),
        ),
        calls,
    )

    def pixabay_leak(*, request, secrets, paths):
        output = paths.root / f"{PEXELS_STOCK_SECRET}.mp4"
        output.write_bytes(b"prefix-" + PIXABAY_STOCK_SECRET.encode() + b"-suffix")
        source_url = "https://pixabay.com/videos/id-999/"
        return {
            "success": True,
            "artifacts": [
                {
                    "path": output,
                    "media_kind": "video",
                    "source_type": "Pixabay",
                    "source_url": source_url,
                    "evidence_kind": "atmosphere",
                    "is_locally_generated": False,
                    "rights_evidence": build_stock_rights_evidence(
                        provider="Pixabay",
                        provider_asset_id="999",
                        media_type="video",
                        asset_page_url=source_url,
                        contributor_name="Pixabay contributor",
                        search_query="office",
                        observed_at="2026-07-18T00:00:00+00:00",
                    ),
                }
            ],
            "selected_artifact": output,
        }

    try:
        result = execute_material_operation(
            request,
            _secrets(request),
            attempt,
            services=MaterialWorkerServices(
                pexels=pexels,
                pixabay=pixabay_leak,
            ),
        )
        encoded = encode_material_worker_result(result)

        assert result.status == "failed"
        assert result.reason_code == "secret_payload_rejected"
        for secret in (PEXELS_STOCK_SECRET, PIXABAY_STOCK_SECRET):
            assert secret.encode() not in encoded
            for path in attempt_path.rglob("*"):
                assert secret not in path.name
                if path.is_file():
                    assert secret.encode() not in path.read_bytes()
    finally:
        attempt.close()


def test_stock_candidates_rejects_batch_with_missing_rights_evidence(
    tmp_path: Path,
) -> None:
    from app.pipeline.material_operation_worker import (
        MaterialWorkerServices,
        execute_material_operation,
    )

    _attempt_path, attempt = _attempt(tmp_path)
    request = _request("stock_candidates")
    calls: list[tuple[str, str, Path]] = []
    services = MaterialWorkerServices(
        pexels=_stock_adapter(
            "Pexels",
            (
                ("candidate-video.mp4", "video", b"pexels-video"),
                ("candidate-image.jpg", "image", b"pexels-image"),
            ),
            calls,
            include_rights=False,
        ),
        pixabay=_stock_adapter(
            "Pixabay",
            (("candidate-best.mp4", "video", b"pixabay-best"),),
            calls,
        ),
    )

    try:
        result = execute_material_operation(
            request,
            _secrets(request),
            attempt,
            services=services,
        )

        assert result.status == "failed"
        assert result.reason_code == "candidate_batch_invalid"
        assert "rights evidence" in result.reason
        assert result.artifacts == ()
    finally:
        attempt.close()


def test_stock_candidates_rejects_secret_bearing_rights_evidence_result_bytes(
    tmp_path: Path,
) -> None:
    from app.materials.stock_rights_evidence import build_stock_rights_evidence
    from app.pipeline.material_operation_protocol import encode_material_worker_result
    from app.pipeline.material_operation_worker import (
        MaterialWorkerServices,
        execute_material_operation,
    )

    _attempt_path, attempt = _attempt(tmp_path)
    request = _request("stock_candidates")
    calls: list[tuple[str, str, Path]] = []

    def pexels_with_leaking_rights(*, request, secrets, paths):
        video = paths.root / "pexels-rights-video.mp4"
        image = paths.root / "pexels-rights-image.jpg"
        video.write_bytes(b"pexels-rights-video")
        image.write_bytes(b"pexels-rights-image")
        page_url = "https://www.pexels.com/video/example-101/"
        return {
            "success": True,
            "artifacts": [
                {
                    "path": video,
                    "media_kind": "video",
                    "source_type": "Pexels",
                    "source_url": page_url,
                    "evidence_kind": "atmosphere",
                    "is_locally_generated": False,
                    "rights_evidence": build_stock_rights_evidence(
                        provider="Pexels",
                        provider_asset_id="101",
                        media_type="video",
                        asset_page_url=page_url,
                        contributor_name="Example Contributor",
                        search_query=f"office {PIXABAY_STOCK_SECRET}",
                        observed_at="2026-07-18T09:30:00+00:00",
                    ),
                },
                {
                    "path": image,
                    "media_kind": "image",
                    "source_type": "Pexels",
                    "source_url": "https://www.pexels.com/photo/example-102/",
                    "evidence_kind": "atmosphere",
                    "is_locally_generated": False,
                },
            ],
        }

    try:
        result = execute_material_operation(
            request,
            _secrets(request),
            attempt,
            services=MaterialWorkerServices(
                pexels=pexels_with_leaking_rights,
                pixabay=_stock_adapter(
                    "Pixabay",
                    (("candidate-best.mp4", "video", b"pixabay-best"),),
                    calls,
                ),
            ),
        )
        encoded = encode_material_worker_result(result)

        assert result.status == "failed"
        for secret in (PEXELS_STOCK_SECRET, PIXABAY_STOCK_SECRET):
            assert secret.encode() not in encoded
    finally:
        attempt.close()


def test_worker_failure_redacts_secret_from_reason_code(tmp_path: Path):
    from app.pipeline.material_operation_worker import MaterialWorkerServices, execute_material_operation

    attempt_path, attempt = _attempt(tmp_path)
    request = _request("pexels")
    secrets = _secrets(request)
    secret = secrets.payload.api_key

    def failed(*, request, secrets, paths):
        return {
            "success": False,
            "reason_code": secret,
            "reason": "provider failed",
        }

    try:
        result = execute_material_operation(
            request,
            secrets,
            attempt,
            services=MaterialWorkerServices(pexels=failed),
        )
        assert result.status == "failed"
        assert secret not in result.reason_code
    finally:
        attempt.close()


def test_success_result_and_staging_names_do_not_contain_secret(tmp_path: Path):
    from app.pipeline.material_operation_worker import MaterialWorkerServices, execute_material_operation

    attempt_path, attempt = _attempt(tmp_path)
    request = _request("pexels")
    secrets = _secrets(request)
    secret = secrets.payload.api_key

    def leaking(*, request, secrets, paths):
        output = paths.root / f"{secret}.mp4"
        output.write_bytes(b"safe-media")
        return {
            "success": True,
            "artifacts": [
                {
                    "path": output,
                    "media_kind": "video",
                    "source_type": secret,
                    "source_url": f"https://example.com/file?key={secret}",
                    "evidence_kind": "atmosphere",
                }
            ],
            "selected_artifact": output,
        }

    try:
        result = execute_material_operation(
            request,
            secrets,
            attempt,
            services=MaterialWorkerServices(pexels=leaking),
        )
        encoded = repr(result).encode()
        assert secret.encode() not in encoded
        for path in attempt_path.rglob("*"):
            assert secret not in path.name
            if path.is_file():
                assert secret.encode() not in path.read_bytes()
    finally:
        attempt.close()


def test_worker_rejects_secret_bytes_in_success_artifact(tmp_path: Path):
    from app.pipeline.material_operation_worker import MaterialWorkerServices, execute_material_operation

    attempt_path, attempt = _attempt(tmp_path)
    request = _request("pexels")
    secrets = _secrets(request)
    secret = secrets.payload.api_key

    def leaking_bytes(*, request, secrets, paths):
        output = paths.root / "media.mp4"
        output.write_bytes(b"prefix-" + secret.encode() + b"-suffix")
        return {
            "success": True,
            "artifacts": [{"path": output, "media_kind": "video"}],
            "selected_artifact": output,
        }

    try:
        result = execute_material_operation(
            request,
            secrets,
            attempt,
            services=MaterialWorkerServices(pexels=leaking_bytes),
        )
        assert result.status == "failed"
        for path in attempt_path.rglob("*"):
            assert secret not in path.name
            if path.is_file():
                assert secret.encode() not in path.read_bytes()
    finally:
        attempt.close()


def test_official_capture_receives_payload_profile_download_and_output_paths(
    tmp_path: Path,
    monkeypatch,
):
    import app.materials.official_capture as capture_module
    import app.pipeline.material_operation_worker as worker

    attempt_path, attempt = _attempt(tmp_path)
    browser_cache = tmp_path / "browser-cache"
    browser_cache.mkdir()
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(browser_cache))
    observed: dict[str, Any] = {}

    class FakeCapture:
        def __init__(self, **kwargs):
            observed.update(kwargs)

        def capture(self, plan):
            observed["plan"] = plan
            frame = plan.output_dir / "frames" / "official_000.png"
            video = plan.output_dir / "capture.mp4"
            frame.parent.mkdir(parents=True, exist_ok=True)
            frame.write_bytes(b"frame")
            video.write_bytes(b"video")
            return SimpleNamespace(
                success=True,
                files=[str(video)],
                scroll_recording_path=str(video),
                used_placeholder=False,
                reason="captured",
            )

    monkeypatch.setattr(capture_module, "OfficialPageCaptureService", FakeCapture)
    request = _request("official_capture")
    try:
        result = worker.execute_material_operation(request, _secrets(request), attempt)
        assert result.status == "succeeded"
        payload = (attempt_path / "payload").resolve()
        for key in ("profile_dir", "downloads_dir"):
            assert observed[key].resolve().is_relative_to(payload)
        assert observed["plan"].output_dir.resolve().is_relative_to(payload)
        assert Path(result.selected_artifact).parts[0] == "payload"
    finally:
        attempt.close()


def test_pixabay_key_reader_never_uses_desktop_or_cwd_fallback(tmp_path: Path, monkeypatch):
    from app.materials.asset_paths import AutoMaterialPathError, AutoMaterialPaths

    paths = AutoMaterialPaths(root=tmp_path / "staging")
    paths.ensure_directories()
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    (desktop / "Pixabay API.env.txt").write_text("PIXABAY_API_KEY=must-not-read", encoding="utf-8")
    (tmp_path / "Pixabay API.env").write_text("PIXABAY_API_KEY=must-not-read-cwd", encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    try:
        with pytest.raises(AutoMaterialPathError) as error:
            paths.read_pixabay_api_key()
        assert "must-not-read" not in str(error.value)
        assert "Desktop" not in str(error.value)
        assert str(paths.pixabay_api_key_path) in str(error.value)
    finally:
        pass


def test_explicit_source_service_key_bypasses_path_lookup(tmp_path: Path, monkeypatch):
    from app.materials.asset_paths import AutoMaterialPaths
    from app.materials.pexels import PexelsDownloadService
    from app.materials.source_clients import PixabayDownloadService

    paths = AutoMaterialPaths(root=tmp_path / "staging")
    paths.ensure_directories()
    monkeypatch.setattr(
        AutoMaterialPaths,
        "read_pexels_api_key",
        lambda _self: (_ for _ in ()).throw(AssertionError("lookup")),
    )
    monkeypatch.setattr(
        AutoMaterialPaths,
        "read_pixabay_api_key",
        lambda _self: (_ for _ in ()).throw(AssertionError("lookup")),
    )

    class PexelsHttp:
        def __init__(self):
            self.headers: list[dict[str, str]] = []

        def get_json(self, url, *, headers, params):
            self.headers.append(headers)
            return {"videos": []} if "videos" in url else {"photos": []}

        def get_bytes(self, url, *, headers):
            raise AssertionError("no download expected")

    class PixabayHttp:
        def __init__(self):
            self.keys: list[str] = []

        def get_json(self, url, *, headers=None, params=None):
            self.keys.append(params["key"])
            return {"hits": []}

        def get_bytes(self, url, *, headers=None):
            raise AssertionError("no download expected")

    pexels_http = PexelsHttp()
    pixabay_http = PixabayHttp()
    pexels = PexelsDownloadService(
        paths=paths,
        api_key="explicit-pexels",
        http_client=pexels_http,
    )
    pixabay = PixabayDownloadService(
        paths=paths,
        api_key="explicit-pixabay",
        http_client=pixabay_http,
    )
    pexels.download_one_video_and_one_photo("query")
    pixabay.download_best("query")
    assert pexels.api_key == "explicit-pexels"
    assert pixabay.api_key == "explicit-pixabay"
    assert pexels_http.headers == [
        {"Authorization": "explicit-pexels"},
        {"Authorization": "explicit-pexels"},
    ]
    assert pixabay_http.keys == ["explicit-pixabay", "explicit-pixabay"]


def test_real_runner_uses_installed_worker_and_keeps_info_motion_in_staging(tmp_path: Path):
    from app.pipeline.material_operation_runner import ProcessMaterialOperationRunner

    root = approve_physical_d_write_root(tmp_path)
    runner = ProcessMaterialOperationRunner(root)
    prepared = runner.prepare_attempt(
        execution_id=EXECUTION_ID,
        operation="info_motion",
    )
    request = MaterialOperationRequest(
        protocol_version=1,
        execution_id=EXECUTION_ID,
        attempt_id=prepared.attempt_id,
        operation="info_motion",
        payload=InfoMotionPayload(project_name="Task 4", title="real worker"),
    )
    secrets = MaterialOperationSecrets(
        version=1,
        execution_id=EXECUTION_ID,
        attempt_id=prepared.attempt_id,
        operation="info_motion",
        payload=InfoMotionSecrets(),
    )
    try:
        outcome = runner.run(
            prepared,
            request,
            secrets=secrets,
            startup_timeout_seconds=30,
            operation_timeout_seconds=120,
        )
        assert outcome.status == "succeeded"
        assert outcome.result is not None
        assert (prepared.directory.path / "ready.json").is_file()
        assert (prepared.directory.path / "result.json").is_file()
        assert not (prepared.directory.path / "container.json").exists()
        selected = prepared.directory.path / Path(outcome.result.selected_artifact)
        assert selected.is_file()
        assert selected.resolve().is_relative_to(
            (prepared.directory.path / "payload").resolve()
        )
        assert outcome.result.artifacts[0].sha256 == hashlib.sha256(
            selected.read_bytes()
        ).hexdigest()
    finally:
        for pin in reversed(prepared.pin_scope):
            pin.close()
        root.close()
