from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.pipeline.timeline_spec import build_timeline_spec_payload
from app.video.timeline_renderer import (
    TimelineRenderError,
    build_timeline_render_plan,
    render_timeline_visual_pilot,
)


def _snapshot(
    assets: list[Path],
    *,
    starts: tuple[float, ...] | None = None,
    candidate_refs: bool = True,
) -> dict:
    starts = starts or tuple(float(index) for index in range(len(assets)))
    segments = []
    for index, asset in enumerate(assets):
        start = starts[index]
        end = start + 1
        segments.append(
            {
                "segment_id": f"shot-{index + 1:02d}",
                "start": f"00:{int(start):02d}",
                "end": f"00:{int(end):02d}",
                "visual_type": "direct_evidence",
                "visual_direction": "测试画面",
                "material_path": str(asset),
                "source_url": "https://example.test/source",
                "evidence_kind": "direct_evidence",
                "material_status": "已锁定",
                "selected_candidate_id": (
                    f"candidate-{index + 1}" if candidate_refs else ""
                ),
            }
        )
    payload = build_timeline_spec_payload(
        segments,
        selected_candidate_ids=(
            {
                item["segment_id"]: item["selected_candidate_id"]
                for item in segments
            }
            if candidate_refs
            else {}
        ),
    )
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
        "utf-8"
    )
    input_hash = hashlib.sha256(encoded).hexdigest()
    return {
        "schema_version": 1,
        "pipeline_id": 7,
        "project_key": "pipeline-7-test",
        "version_id": f"ts-7-0001-{input_hash[:12]}",
        "version_number": 1,
        "input_hash": input_hash,
        "tracks": payload["tracks"],
        "segments": payload["segments"],
        "settings": payload["settings"],
        "source": "test",
        "created_at": "2026-07-18T00:00:00+00:00",
    }


def _asset(tmp_path: Path, name: str = "source.mp4") -> Path:
    path = tmp_path / name
    path.write_bytes(b"not-a-video-but-a-regular-file")
    return path


def test_plan_is_pure_and_contains_visual_only_ffmpeg_steps(tmp_path: Path):
    first = _asset(tmp_path, "first.mp4")
    second = _asset(tmp_path, "second.png")
    snapshot = _snapshot([first, second])
    output_root = tmp_path / "renderer-pilot"

    plan = build_timeline_render_plan(snapshot, output_root=output_root)

    assert plan.mode == "visual_only_pilot"
    assert plan.version_id == snapshot["version_id"]
    assert plan.output_path == output_root / snapshot["version_id"] / "rendered.mp4"
    assert not output_root.exists(), "dry-run planning must not create output files"
    assert len(plan.segment_commands) == 2
    assert "scale=1080:1920" in " ".join(plan.segment_commands[0])
    assert "-stream_loop" in plan.segment_commands[0]
    assert "-loop" in plan.segment_commands[1]
    assert plan.concat_command[plan.concat_command.index("-f") + 1] == "concat"
    assert "-n" in plan.concat_command
    assert all(str(path).upper().startswith("D:") for path in [plan.output_path, plan.staging_dir])
    json.dumps(plan.to_dict(), ensure_ascii=False)


def test_plan_rejects_non_d_material_and_non_contiguous_timeline(tmp_path: Path):
    valid = _asset(tmp_path)
    with pytest.raises(TimelineRenderError, match="D 盘"):
        build_timeline_render_plan(
            _snapshot([valid]),
            output_root=Path("C:/not-allowed/renderer-pilot"),
        )

    with pytest.raises(TimelineRenderError, match="D 盘"):
        build_timeline_render_plan(
            _snapshot([Path("C:/not-allowed/source.mp4")]),
            output_root=tmp_path / "renderer-pilot",
        )

    with pytest.raises(TimelineRenderError, match="连续"):
        build_timeline_render_plan(
            _snapshot([valid, _asset(tmp_path, "later.mp4")], starts=(0, 2)),
            output_root=tmp_path / "renderer-pilot",
        )


def test_plan_rejects_tampered_snapshot_and_missing_candidate_reference(tmp_path: Path):
    valid = _asset(tmp_path)
    snapshot = _snapshot([valid])
    tampered = dict(snapshot)
    tampered["segments"] = [dict(snapshot["segments"][0], material_path="D:/other.mp4")]
    with pytest.raises(TimelineRenderError, match="哈希"):
        build_timeline_render_plan(tampered, output_root=tmp_path / "renderer-pilot")

    no_candidate = _snapshot([valid], candidate_refs=False)
    with pytest.raises(TimelineRenderError, match="候选"):
        build_timeline_render_plan(no_candidate, output_root=tmp_path / "renderer-pilot")


def test_render_pilot_keeps_old_output_untouched_and_publishes_new_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    valid = _asset(tmp_path)
    snapshot = _snapshot([valid])
    output_root = tmp_path / "renderer-pilot"
    output_root.mkdir()
    sentinel = output_root / "old-output-sentinel.txt"
    sentinel.write_text("must remain", encoding="utf-8")
    calls: list[tuple[str, ...]] = []

    def fake_run(command: tuple[str, ...], *, cwd: Path, timeout_seconds: float) -> None:
        calls.append(command)
        output = Path(command[-1])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"pilot-output")

    monkeypatch.setattr("app.video.timeline_renderer._run_ffmpeg", fake_run)
    result = render_timeline_visual_pilot(snapshot, output_root=output_root)

    assert result.mode == "visual_only_pilot"
    assert result.output_path.is_file()
    assert result.output_path.read_bytes() == b"pilot-output"
    assert len(calls) == 2
    assert result.manifest_path.is_file()
    assert result.plan_path.is_file()
    assert result.output_path.suffix == ".mp4"
    assert sentinel.read_text(encoding="utf-8") == "must remain"
    with pytest.raises(TimelineRenderError, match="拒绝覆盖|已有文件"):
        render_timeline_visual_pilot(snapshot, output_root=output_root)


def test_partial_render_reuses_unchanged_segment_and_rebuilds_only_changed_segment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    first_asset = _asset(tmp_path, "first.mp4")
    second_asset = _asset(tmp_path, "second.mp4")
    replacement_asset = _asset(tmp_path, "replacement.mp4")
    replacement_asset.write_bytes(b"replacement-material")
    output_root = tmp_path / "renderer-pilot"
    calls: list[tuple[str, ...]] = []

    def fake_run(command: tuple[str, ...], *, cwd: Path, timeout_seconds: float) -> None:
        calls.append(command)
        output = Path(command[-1])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(output.name.encode("utf-8"))

    monkeypatch.setattr("app.video.timeline_renderer._run_ffmpeg", fake_run)
    first = render_timeline_visual_pilot(
        _snapshot([first_asset, second_asset]),
        output_root=output_root,
    )
    calls.clear()

    rebuilt = render_timeline_visual_pilot(
        _snapshot([first_asset, replacement_asset]),
        output_root=output_root,
        reuse_from_manifest=first.render_manifest_path,
    )

    assert rebuilt.reused_segment_ids == ("shot-01",)
    assert rebuilt.rebuilt_segment_ids == ("shot-02",)
    assert rebuilt.source_version_id == first.version_id
    assert rebuilt.render_manifest_path.is_file()
    assert len(calls) == 2, "only the changed segment plus final concat should run FFmpeg"
    assert "shot-02" in Path(calls[0][-1]).name
    assert calls[1][-1].endswith("rendered.part.mp4")

    first_stage = first.output_path.parent / "staging" / "0001-shot-01.mp4"
    rebuilt_stage = rebuilt.output_path.parent / "staging" / "0001-shot-01.mp4"
    assert rebuilt_stage.read_bytes() == first_stage.read_bytes()
    assert rebuilt_stage != first_stage
