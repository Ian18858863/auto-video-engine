from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone

import pytest

from app.materials.pexels import _candidate_from_photo, _candidate_from_video
from app.materials.source_clients import _pixabay_photo_candidate
from app.materials.stock_rights_evidence import (
    StockRightsEvidenceError,
    build_stock_rights_evidence,
    normalise_stock_rights_evidence,
    review_stock_rights_evidence,
)


OBSERVED_AT = datetime(2026, 7, 18, 9, 30, tzinfo=timezone.utc)


def test_builds_public_pexels_evidence_without_cdn_or_raw_api_secrets():
    evidence = build_stock_rights_evidence(
        provider="Pexels",
        provider_asset_id="123",
        media_type="video",
        asset_page_url="https://www.pexels.com/video/example-123/",
        contributor_id="77",
        contributor_name="Ada",
        contributor_profile_url="https://www.pexels.com/@ada/",
        search_query="AI office",
        observed_at=OBSERVED_AT,
    )

    assert evidence["schema_version"] == 1
    assert evidence["provider"] == "Pexels"
    assert evidence["asset_page_url"] == "https://www.pexels.com/video/example-123/"
    assert evidence["contributor"] == {
        "provider_user_id": "77",
        "name": "Ada",
        "profile_url": "https://www.pexels.com/@ada/",
    }
    assert evidence["license_snapshot"]["license_name"] == "Pexels License"
    assert evidence["attribution"]["license_required"] is False
    assert "Pexels" in evidence["attribution"]["api_display_requirement"]
    assert evidence["rights_review"]["status"] == "unreviewed"
    encoded = repr(evidence).casefold()
    assert "download_url" not in encoded
    assert "raw_api" not in encoded
    assert "api_key" not in encoded


def test_rejects_provider_page_that_is_really_a_cdn_or_lookalike():
    with pytest.raises(StockRightsEvidenceError, match="asset_page_url"):
        build_stock_rights_evidence(
            provider="Pixabay",
            provider_asset_id="88",
            media_type="photo",
            asset_page_url="https://pixabay.com.evil.test/photos/88/",
            contributor_name="Lin",
            search_query="office",
            observed_at=OBSERVED_AT,
        )


def test_clearance_cannot_be_claimed_while_review_fields_are_unknown():
    evidence = build_stock_rights_evidence(
        provider="Pixabay",
        provider_asset_id="88",
        media_type="photo",
        asset_page_url="https://pixabay.com/photos/example-88/",
        contributor_name="Lin",
        search_query="office",
        observed_at=OBSERVED_AT,
    )
    tampered = deepcopy(evidence)
    tampered["rights_review"]["status"] = "cleared_for_intended_use"
    tampered["rights_review"]["reviewed_by"] = "Ian"
    tampered["rights_review"]["reviewed_at"] = "2026-07-18T10:00:00+00:00"

    with pytest.raises(StockRightsEvidenceError, match="unknown"):
        normalise_stock_rights_evidence(tampered)


def test_pexels_api_candidates_keep_asset_page_and_contributor_evidence():
    video = _candidate_from_video(
        {
            "id": 101,
            "url": "https://www.pexels.com/video/example-101/",
            "duration": 8,
            "user": {
                "id": 7,
                "name": "Video Author",
                "url": "https://www.pexels.com/@video-author/",
            },
            "video_files": [
                {
                    "id": 1,
                    "file_type": "video/mp4",
                    "link": "https://videos.pexels.com/video-101.mp4",
                    "width": 1080,
                    "height": 1920,
                }
            ],
        },
        "office",
    )
    photo = _candidate_from_photo(
        {
            "id": 202,
            "url": "https://www.pexels.com/photo/example-202/",
            "width": 1920,
            "height": 1280,
            "photographer": "Photo Author",
            "photographer_id": 8,
            "photographer_url": "https://www.pexels.com/@photo-author/",
            "src": {"large2x": "https://images.pexels.com/photo-202.jpg"},
        },
        "office",
    )

    assert video is not None and photo is not None
    assert video.source_url == "https://www.pexels.com/video/example-101/"
    assert video.rights_evidence["contributor"]["name"] == "Video Author"
    assert photo.source_url == "https://www.pexels.com/photo/example-202/"
    assert photo.rights_evidence["provider_asset_id"] == "202"


def test_pixabay_api_candidate_keeps_page_author_and_license_snapshot():
    candidate = _pixabay_photo_candidate(
        {
            "id": 303,
            "pageURL": "https://pixabay.com/photos/example-303/",
            "user_id": 9,
            "user": "Pixabay Author",
            "largeImageURL": "https://cdn.pixabay.com/photo-303.jpg",
            "imageWidth": 1920,
            "imageHeight": 1280,
        },
        "office",
    )

    assert candidate is not None
    assert candidate.rights_evidence["provider"] == "Pixabay"
    assert candidate.rights_evidence["contributor"]["provider_user_id"] == "9"
    assert candidate.rights_evidence["license_snapshot"]["license_name"] == "Pixabay Content License"


def test_per_use_review_can_clear_only_with_complete_server_review_metadata():
    evidence = build_stock_rights_evidence(
        provider="Pexels",
        provider_asset_id="123",
        media_type="photo",
        asset_page_url="https://www.pexels.com/photo/example-123/",
        contributor_name="Ada",
        search_query="office",
        observed_at=OBSERVED_AT,
    )

    reviewed = review_stock_rights_evidence(
        evidence,
        status="cleared_for_intended_use",
        intended_use="short-video product demo",
        identifiable_people="no",
        brands_or_logos="no",
        property_or_architecture="no",
        music_or_third_party_audio="no",
        third_party_clearance="not_required",
        restriction_notes=["Only use in the approved product demo."],
        reviewed_by="Ian",
        reviewed_at="2026-07-18T10:00:00+00:00",
    )

    assert evidence["rights_review"]["status"] == "unreviewed"
    assert reviewed["rights_review"] == {
        "status": "cleared_for_intended_use",
        "intended_use": "short-video product demo",
        "identifiable_people": "no",
        "brands_or_logos": "no",
        "property_or_architecture": "no",
        "music_or_third_party_audio": "no",
        "third_party_clearance": "not_required",
        "restriction_notes": ["Only use in the approved product demo."],
        "reviewed_by": "Ian",
        "reviewed_at": "2026-07-18T10:00:00+00:00",
    }


def test_per_use_review_rejects_false_clearance_and_unattributed_needs_review():
    evidence = build_stock_rights_evidence(
        provider="Pixabay",
        provider_asset_id="88",
        media_type="photo",
        asset_page_url="https://pixabay.com/photos/example-88/",
        contributor_name="Lin",
        search_query="office",
        observed_at=OBSERVED_AT,
    )

    with pytest.raises(StockRightsEvidenceError, match="third.party|clearance"):
        review_stock_rights_evidence(
            evidence,
            status="cleared_for_intended_use",
            intended_use="short-video ad",
            identifiable_people="yes",
            brands_or_logos="no",
            property_or_architecture="no",
            music_or_third_party_audio="no",
            third_party_clearance="required",
            restriction_notes=[],
            reviewed_by="Ian",
            reviewed_at="2026-07-18T10:00:00+00:00",
        )

    tampered = deepcopy(evidence)
    tampered["rights_review"].update(
        {
            "status": "needs_review",
            "reviewed_by": "",
            "reviewed_at": "",
        }
    )
    with pytest.raises(StockRightsEvidenceError, match="reviewed_by|reviewed_at"):
        normalise_stock_rights_evidence(tampered)
