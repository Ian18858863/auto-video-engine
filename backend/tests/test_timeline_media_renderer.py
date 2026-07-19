from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import wave

import pytest

from app.pipeline.timeline_spec import _sha256, build_timeline_spec_payload, load_timeline_spec_snapshot
from app.pipeline.timeline_media_service import _public_render_result
from app.video.timeline_media_renderer import (
    TimelineMediaRenderError,
    build_timeline_media_render_plan,
    render_timeline_media,
)
from app.video.timeline_renderer import TimelineRenderResult


def _wav(path: Path, duration: float = 1.0) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(b"\0\0" * int(8000 * duration))


def _snapshot(project: Path, *, audio_duration: float = 1.0):
    audio = project / "素材信息" / "音频字幕版本" / "voiceover.wav"
    subtitle = project / "素材信息" / "音频字幕版本" / "subtitles.srt"
    material = project / "素材信息" / "候选.jpg"
    audio.parent.mkdir(parents=True, exist_ok=True)
    _wav(audio, audio_duration)
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\n测试\n", encoding="utf-8")
    material.write_bytes(b"image")
    import hashlib

    def digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    payload = build_timeline_spec_payload(
        [
            {
                "segment_id": "shot-01",
                "start": "00:00",
                "end": "00:01",
                "material_path": str(material),
            }
        ],
        selected_candidate_ids={"shot-01": "cand-1"},
        artifacts={
            "voiceover": {
                "project_relative_path": "素材信息/音频字幕版本/voiceover.wav",
                "sha256": digest(audio),
                "duration_seconds": audio_duration,
                "mime": "audio/wav",
            },
            "subtitles": {
                "project_relative_path": "素材信息/音频字幕版本/subtitles.srt",
                "sha256": digest(subtitle),
                "mime": "application/x-subrip",
            },
        },
    )
    input_hash = _sha256(payload)
    return load_timeline_spec_snapshot(
        {
            "schema_version": 1,
            "pipeline_id": 1,
            "project_key": "p-1-media-test",
            "version_id": f"ts-1-0001-{input_hash[:12]}",
            "version_number": 1,
            "input_hash": input_hash,
            **payload,
            "source": "test",
            "created_at": "2026-07-18T00:00:00+00:00",
        }
    )


def test_media_plan_maps_real_audio_and_subtitle_tracks(tmp_path: Path):
    project = tmp_path / "D" / "project"
    output = tmp_path / "D" / "renders"
    project.mkdir(parents=True)
    snapshot = _snapshot(project)

    plan = build_timeline_media_render_plan(
        snapshot,
        project_dir=project,
        output_root=output,
        ffmpeg_executable="D:/tools/ffmpeg.exe",
    )

    assert plan.audio_duration_seconds == pytest.approx(1.0)
    assert "-map" in plan.mux_command
    assert "0:v:0" in plan.mux_command
    assert "1:a:0" in plan.mux_command
    assert "2:0" in plan.mux_command
    assert "-shortest" not in plan.mux_command
    assert "-af" not in plan.mux_command


def test_media_plan_fails_closed_on_duration_or_hash_mismatch(tmp_path: Path):
    project = tmp_path / "D" / "project"
    project.mkdir(parents=True)
    snapshot = _snapshot(project, audio_duration=2.0)
    with pytest.raises(TimelineMediaRenderError, match="duration_mismatch"):
        build_timeline_media_render_plan(
            snapshot,
            project_dir=project,
            output_root=tmp_path / "D" / "renders",
            ffmpeg_executable="D:/tools/ffmpeg.exe",
        )

    project2 = tmp_path / "D" / "project2"
    project2.mkdir(parents=True)
    valid = _snapshot(project2)
    raw = valid.to_dict()
    raw["artifacts"]["voiceover"]["sha256"] = "f" * 64
    normalized_payload = {
        key: raw[key] for key in ("tracks", "segments", "settings", "artifacts")
    }
    raw["input_hash"] = _sha256(normalized_payload)
    raw["version_id"] = f"ts-1-0001-{raw['input_hash'][:12]}"
    with pytest.raises(TimelineMediaRenderError, match="hash_mismatch"):
        build_timeline_media_render_plan(
            raw,
            project_dir=project2,
            output_root=tmp_path / "D" / "renders2",
            ffmpeg_executable="D:/tools/ffmpeg.exe",
        )


def test_media_render_passes_visual_reuse_and_binds_final_to_timeline_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    project = tmp_path / "D" / "project"
    output = project / "成片" / "versions"
    project.mkdir(parents=True)
    snapshot = _snapshot(project)
    reuse_manifest = project / "成片" / "old" / "render-manifest.json"
    captured: dict[str, object] = {}

    def fake_visual(snapshot_arg, *, output_root, reuse_from_manifest=None, **_kwargs):
        captured["reuse_from_manifest"] = reuse_from_manifest
        version_dir = Path(output_root) / snapshot_arg.version_id
        staging = version_dir / "staging"
        staging.mkdir(parents=True)
        stage = staging / "0001-shot-01.mp4"
        stage.write_bytes(b"stage")
        visual_output = version_dir / "rendered.mp4"
        visual_output.write_bytes(b"visual")
        concat = version_dir / "concat.txt"
        concat.write_text("file 'stage'\n", encoding="utf-8")
        plan = version_dir / "plan.json"
        plan.write_text("{}\n", encoding="utf-8")
        manifest = version_dir / "render-manifest.json"
        manifest.write_text("{}\n", encoding="utf-8")
        return TimelineRenderResult(
            version_id=snapshot_arg.version_id,
            mode="visual_only_pilot",
            output_path=visual_output,
            manifest_path=concat,
            plan_path=plan,
            render_manifest_path=manifest,
            reused_segment_ids=("shot-01",),
            rebuilt_segment_ids=(),
            source_version_id="ts-1-0001-old",
        )

    def fake_mux(command, *, cwd, timeout_seconds):
        Path(command[-1]).write_bytes(b"final-media")

    monkeypatch.setattr(
        "app.video.timeline_media_renderer.render_timeline_visual_pilot",
        fake_visual,
    )
    monkeypatch.setattr("app.video.timeline_media_renderer._run_command", fake_mux)
    monkeypatch.setattr(
        "app.video.timeline_media_renderer.probe_audio_subtitles",
        lambda *_args, **_kwargs: SimpleNamespace(to_dict=lambda: {"passed": True}),
    )
    monkeypatch.setattr(
        "app.video.timeline_media_renderer.probe_video",
        lambda *_args, **_kwargs: SimpleNamespace(to_dict=lambda: {"passed": True}),
    )

    result = render_timeline_media(
        snapshot,
        project_dir=project,
        output_root=output,
        ffmpeg_executable="D:/tools/ffmpeg.exe",
        ffprobe_executable="D:/tools/ffprobe.exe",
        reuse_from_visual_manifest=reuse_manifest,
    )

    assert captured["reuse_from_manifest"] == reuse_manifest
    assert result.timeline_version_id == snapshot.version_id
    assert result.reused_segment_ids == ("shot-01",)
    assert result.rebuilt_segment_ids == ()
    assert result.artifact_sha256 == hashlib.sha256(b"final-media").hexdigest()
    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    assert report["timeline_version_id"] == snapshot.version_id
    assert report["timeline_input_hash"] == snapshot.input_hash
    assert report["artifact_sha256"] == result.artifact_sha256
    assert report["visual_rebuild"]["reused_segment_ids"] == ["shot-01"]
    public = _public_render_result(result, project_dir=project)
    assert public["timeline_version_id"] == snapshot.version_id
    assert public["visual_rebuild"]["reused_segment_ids"] == ["shot-01"]
    assert str(project) not in json.dumps(public, ensure_ascii=False)
