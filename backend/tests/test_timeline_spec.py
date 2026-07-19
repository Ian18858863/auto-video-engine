from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from app.models import ProductionPipeline
from app.pipeline.service import build_project_from_hotspot
from app.pipeline.timeline_spec import (
    TimelineSpecError,
    TimelineSpecLedger,
    build_timeline_spec_payload,
)
from tests.test_production_pipeline import add_hot_item, make_session, prepared_material_root


def _pipeline(tmp_path: Path):
    with make_session() as session:
        built = build_project_from_hotspot(
            session,
            add_hot_item(session).id,
            project_name=f"timeline-{uuid4().hex[:10]}",
            material_root=prepared_material_root(tmp_path),
        )
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        assert pipeline is not None
        # Keep the session and pipeline alive long enough for the caller's
        # ledger operation; the SQLite test object is otherwise detached.
        return pipeline, prepared_material_root(tmp_path) / "项目"


def test_timeline_spec_is_append_only_idempotent_and_reopenable(tmp_path: Path):
    pipeline, project_root = _pipeline(tmp_path)
    ledger = TimelineSpecLedger(pipeline, project_root=project_root)
    before_ids = {item.version_id for item in ledger.list()}
    payload = build_timeline_spec_payload(
        [
            {
                "segment_id": "shot-01",
                "start": "00:00",
                "end": "00:03",
                "visual_type": "direct_evidence",
                "visual_direction": "证据页面",
                "material_path": "D:/素材/shot-01.mp4",
                "source_url": "https://example.test/source",
            }
        ],
        selected_candidate_ids={"shot-01": "cand-1"},
    )

    first = ledger.create(payload)
    same = ledger.create(payload)
    changed = ledger.create(
        build_timeline_spec_payload(
            [
                {
                    "segment_id": "shot-01",
                    "start": "00:00",
                    "end": "00:04",
                    "visual_type": "direct_evidence",
                    "visual_direction": "证据页面",
                    "material_path": "D:/素材/shot-01.mp4",
                    "source_url": "https://example.test/source",
                }
            ],
            selected_candidate_ids={"shot-01": "cand-1"},
        )
    )

    assert same.version_id == first.version_id
    assert changed.version_number == first.version_number + 1
    after_ids = {item.version_id for item in ledger.list()}
    assert after_ids == before_ids | {first.version_id, changed.version_id}
    assert ledger.latest().version_id == changed.version_id
    assert (
        Path(pipeline.project_dir)
        / "时间线"
        / "versions"
        / f"{first.version_id}.json"
    ).is_file()
    assert changed.segments[0]["clip_id"] == "shot-01:visual"
    assert changed.segments[0]["selected_candidate_id"] == "cand-1"

    reopened = TimelineSpecLedger(pipeline, project_root=project_root).latest()
    assert reopened.version_id == changed.version_id
    assert reopened.to_dict() == changed.to_dict()


def test_timeline_spec_rejects_unstable_or_malformed_segments(tmp_path: Path):
    pipeline, project_root = _pipeline(tmp_path)
    ledger = TimelineSpecLedger(pipeline, project_root=project_root)

    with pytest.raises(TimelineSpecError, match="segment_id"):
        ledger.create({"segments": [{"start": "00:00", "end": "00:01"}]})

    with pytest.raises(TimelineSpecError, match="end"):
        ledger.create(
            build_timeline_spec_payload(
                [
                    {
                        "segment_id": "shot-01",
                        "start": "00:02",
                        "end": "00:01",
                    }
                ]
            )
        )


def test_timeline_spec_preserves_numeric_zero_start_time():
    payload = build_timeline_spec_payload(
        [
            {
                "segment_id": "shot-01",
                "start": 0,
                "end": 1,
                "material_path": "D:/素材/shot-01.mp4",
            }
        ]
    )

    assert payload["segments"][0]["start"] == "0"
    assert payload["segments"][0]["start_seconds"] == 0.0


def test_timeline_spec_preserves_hashed_audio_and_subtitle_artifacts():
    payload = build_timeline_spec_payload(
        [
            {
                "segment_id": "shot-01",
                "start": "00:00",
                "end": "00:01",
            }
        ],
        artifacts={
            "artifact_id": "as-1-0000000000000000",
            "voiceover": {
                "project_relative_path": "素材信息/音频字幕版本/as-1-0000000000000000/voiceover.wav",
                "sha256": "a" * 64,
                "duration_seconds": 1.25,
                "mime": "audio/wav",
            },
            "subtitles": {
                "project_relative_path": "素材信息/音频字幕版本/as-1-0000000000000000/subtitles.srt",
                "sha256": "b" * 64,
                "mime": "application/x-subrip",
            },
        },
    )

    assert payload["artifacts"]["voiceover"]["duration_seconds"] == 1.25
    assert "D:" not in str(payload["artifacts"])
