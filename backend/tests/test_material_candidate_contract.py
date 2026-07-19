from __future__ import annotations

from pathlib import Path

import pytest

from app.materials.stock_rights_evidence import (
    build_stock_rights_evidence,
    review_stock_rights_evidence,
)
from app.pipeline import material_candidate_contract as candidate_contract
from app.pipeline.material_candidate_contract import (
    CandidateContractError,
    CandidateLockedError,
    CandidateSet,
    lock_candidate,
    normalize_candidate,
    replace_candidate,
    reject_candidate,
    update_candidate_rights_evidence,
    validate_candidate_set,
)


def _raw_candidate(
    preview: Path,
    index: int,
    *,
    source: str = "pexels",
    status: str = "candidate",
    reason: str = "matches the shot intent",
) -> dict[str, str]:
    return {
        "candidate_id": f"cand-{index}",
        "source": source,
        "preview_path": str(preview),
        "url": f"https://example.test/assets/{index}",
        "rights": "commercial-use terms captured",
        "evidence_kind": "atmosphere",
        "status": status,
        "reason": reason,
    }


def _candidate_set(tmp_path: Path) -> CandidateSet:
    rows = []
    for index in range(1, 4):
        preview = tmp_path / f"preview-{index}.jpg"
        preview.write_bytes(b"preview")
        rows.append(
            _raw_candidate(
                preview,
                index,
                source="pexels" if index < 3 else "pixabay",
            )
        )
    return CandidateSet.from_candidates("shot-01", rows)


def test_normalize_candidate_keeps_explicit_contract_fields(tmp_path: Path):
    preview = tmp_path / "preview.jpg"
    preview.write_bytes(b"preview")

    candidate = normalize_candidate(
        {
            "candidate_id": "  cand-01 ",
            "source": " Pexels ",
            "preview_path": preview,
            "url": " https://example.test/video/1 ",
            "rights": " commercial-use terms captured ",
            "evidence_kind": " atmosphere ",
            "status": " candidate ",
            "reason": " clean office frame ",
        }
    )

    assert candidate.candidate_id == "cand-01"
    assert candidate.source == "Pexels"
    assert candidate.preview_path == str(preview)
    assert candidate.url == "https://example.test/video/1"
    assert candidate.rights == "commercial-use terms captured"
    assert candidate.evidence_kind == "atmosphere"
    assert candidate.status == "candidate"
    assert candidate.reason == "clean office frame"


def test_candidate_round_trips_provider_rights_evidence_and_binds_source_page(
    tmp_path: Path,
):
    preview = tmp_path / "provider.jpg"
    preview.write_bytes(b"preview")
    rights_evidence = build_stock_rights_evidence(
        provider="Pexels",
        provider_asset_id="101",
        media_type="photo",
        asset_page_url="https://www.pexels.com/photo/example-101/",
        contributor_name="Ada",
        search_query="office",
        observed_at="2026-07-18T09:30:00+00:00",
    )
    raw = {
        "candidate_id": "pexels-101",
        "source": "Pexels",
        "preview_path": str(preview),
        "url": "https://www.pexels.com/photo/example-101/",
        "rights": "Pexels License",
        "evidence_kind": "atmosphere",
        "status": "candidate",
        "reason": "semantic match",
        "rights_evidence": rights_evidence,
    }

    candidate = normalize_candidate(raw)

    assert candidate.rights_evidence == rights_evidence
    assert candidate.to_dict()["rights_evidence"]["rights_review"]["status"] == "unreviewed"

    mismatched = dict(raw, url="https://www.pexels.com/photo/other-999/")
    with pytest.raises(CandidateContractError, match="asset_page_url"):
        normalize_candidate(mismatched)


def test_candidate_set_requires_three_to_five_real_previews(tmp_path: Path):
    preview = tmp_path / "only-preview.jpg"
    preview.write_bytes(b"preview")
    too_few = [_raw_candidate(preview, 1), _raw_candidate(preview, 2)]

    with pytest.raises(CandidateContractError, match="3.*5"):
        validate_candidate_set("shot-01", too_few)

    missing_preview = [
        _raw_candidate(
            tmp_path / f"p-{i}.jpg",
            i,
            source="pexels" if i < 3 else "pixabay",
        )
        for i in range(1, 4)
    ]
    with pytest.raises(CandidateContractError, match="preview"):
        validate_candidate_set("shot-01", missing_preview)


def test_candidate_set_rejects_three_rows_from_one_source_by_default(tmp_path: Path):
    rows = []
    for index in range(1, 4):
        preview = tmp_path / f"same-{index}.jpg"
        preview.write_bytes(b"preview")
        rows.append(_raw_candidate(preview, index, source="pexels"))

    with pytest.raises(CandidateContractError, match="source"):
        validate_candidate_set("shot-01", rows)


def test_replace_is_pure_and_locked_candidate_cannot_be_silently_overwritten(
    tmp_path: Path,
):
    state = _candidate_set(tmp_path)
    replacement_preview = tmp_path / "replacement.jpg"
    replacement_preview.write_bytes(b"preview")
    replacement = _raw_candidate(replacement_preview, 4, source="pixabay")

    replaced = replace_candidate(state, "cand-1", replacement)

    assert state.candidates[0].candidate_id == "cand-1"
    assert replaced.candidates[0].candidate_id == "cand-4"
    assert replaced.candidates[0].source == "pixabay"

    locked = lock_candidate(replaced, "cand-4", reason="Ian selected this frame")
    with pytest.raises(CandidateLockedError, match="locked"):
        replace_candidate(locked, "cand-4", _raw_candidate(replacement_preview, 5))

    assert locked.locked_candidate_id == "cand-4"
    assert replaced.locked_candidate_id is None


def test_lock_and_reject_are_pure_and_reject_requires_a_reason(tmp_path: Path):
    state = _candidate_set(tmp_path)
    locked = lock_candidate(state, "cand-2", reason="best visual match")

    assert state.locked_candidate_id is None
    assert locked.locked_candidate_id == "cand-2"
    assert locked.get("cand-2").status == "locked"

    rejected = reject_candidate(locked, "cand-1", reason="wrong subject")
    assert rejected.get("cand-1").status == "rejected"
    assert rejected.get("cand-1").reason == "wrong subject"
    assert locked.get("cand-1").status == "candidate"

    with pytest.raises(CandidateContractError, match="reason"):
        reject_candidate(state, "cand-1", reason="")
    with pytest.raises(CandidateLockedError, match="locked"):
        reject_candidate(locked, "cand-2", reason="changed mind")


def test_reselect_is_explicit_and_keeps_ordinary_locked_replace_protection(
    tmp_path: Path,
):
    original = lock_candidate(
        _candidate_set(tmp_path),
        "cand-1",
        reason="Ian approved the first framing",
    )

    changed = candidate_contract.reselect_candidate(
        original,
        "cand-2",
        reason="The second candidate better supports the narration",
    )

    assert original.locked_candidate_id == "cand-1"
    assert changed.locked_candidate_id == "cand-2"
    assert changed.get("cand-1").status == "candidate"
    assert changed.get("cand-2").status == "locked"
    assert changed.revision == original.revision + 1
    with pytest.raises(CandidateContractError, match="reason"):
        candidate_contract.reselect_candidate(original, "cand-2", reason="")
    with pytest.raises(CandidateLockedError, match="locked"):
        replace_candidate(
            original,
            "cand-1",
            _raw_candidate(tmp_path / "unused.jpg", 4),
        )


def test_rights_review_updates_even_a_locked_candidate_without_changing_identity(
    tmp_path: Path,
):
    state = _candidate_set(tmp_path)
    pexels = state.get("cand-1")
    evidence = build_stock_rights_evidence(
        provider="Pexels",
        provider_asset_id="101",
        media_type="photo",
        asset_page_url="https://www.pexels.com/photo/example-101/",
        contributor_name="Ada",
        search_query="office",
        observed_at="2026-07-18T09:30:00+00:00",
    )
    rows = list(state.candidates)
    rows[0] = candidate_contract.CandidateEntry(
        candidate_id=pexels.candidate_id,
        source="Pexels",
        preview_path=pexels.preview_path,
        url=evidence["asset_page_url"],
        rights=evidence["license_snapshot"]["license_name"],
        evidence_kind=pexels.evidence_kind,
        status=pexels.status,
        reason=pexels.reason,
        rights_evidence=evidence,
    )
    with_evidence = CandidateSet(state.shot_id, tuple(rows), revision=state.revision)
    locked = lock_candidate(with_evidence, "cand-1", reason="best framing")
    reviewed = review_stock_rights_evidence(
        evidence,
        status="cleared_for_intended_use",
        intended_use="short-video product demo",
        identifiable_people="no",
        brands_or_logos="no",
        property_or_architecture="no",
        music_or_third_party_audio="no",
        third_party_clearance="not_required",
        restriction_notes=[],
        reviewed_by="Ian",
        reviewed_at="2026-07-18T10:00:00+00:00",
    )

    changed = update_candidate_rights_evidence(locked, "cand-1", reviewed)

    assert changed.revision == locked.revision + 1
    assert changed.locked_candidate_id == "cand-1"
    assert changed.get("cand-1").rights_evidence == reviewed
    assert changed.get("cand-1").preview_path == locked.get("cand-1").preview_path
    assert locked.get("cand-1").rights_evidence["rights_review"]["status"] == "unreviewed"
