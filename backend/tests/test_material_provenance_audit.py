from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[1]
DATABASE_AUDIT_KEYS = {
    "blockers",
    "canonical_database_path",
    "canonical_exists",
    "canonical_size_bytes",
    "foreign_key_violations",
    "inspected_candidate_paths",
    "quick_check",
    "table_counts",
    "warnings",
}


def _provenance_module():
    spec = importlib.util.find_spec("app.materials.provenance")
    assert spec is not None, "Task 5 requires one provenance module"
    return importlib.import_module("app.materials.provenance")


def _create_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY)")
        connection.commit()


def _run_audit_cli(database: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "app.maintenance.phase0_migration",
            "audit",
            "--database",
            str(database),
            *extra,
            "--json",
        ],
        cwd=BACKEND_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )


def test_capture_provenance_distinguishes_real_http_placeholder_and_file() -> None:
    provenance = _provenance_module()

    direct = provenance.classify_capture_provenance(
        "https://example.com/news/1",
        used_placeholder=False,
    )
    fallback = provenance.classify_capture_provenance(
        "https://example.com/news/1",
        used_placeholder=True,
    )
    local = provenance.classify_capture_provenance(
        "file:///D:/Temp/explanation.html",
        used_placeholder=False,
    )

    assert direct.evidence_kind == "direct_evidence"
    assert direct.source_url == "https://example.com/news/1"
    assert direct.is_locally_generated is False
    assert fallback.evidence_kind == "explanation"
    assert fallback.source_url == "https://example.com/news/1"
    assert fallback.is_locally_generated is True
    assert local.evidence_kind == "explanation"
    assert local.source_url == ""
    assert local.is_locally_generated is True


def test_legacy_fake_official_report_is_read_only_and_listed(tmp_path: Path) -> None:
    provenance = _provenance_module()
    report = tmp_path / "成片报告.json"
    report.write_text(
        json.dumps(
            {
                "素材匹配": [
                    {
                        "素材类型": "官方证据画面",
                        "素材路径": str(tmp_path / "legacy.mp4"),
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    video = tmp_path / "legacy.mp4"
    video.write_bytes(b"historical-video")
    before_bytes = report.read_bytes()
    before_hash = hashlib.sha256(before_bytes).hexdigest()
    before_stat = report.stat()
    before_video = video.read_bytes()

    findings = provenance.audit_legacy_material_reports(report_files=[report.resolve()])

    assert [(item.status, Path(item.path)) for item in findings] == [
        ("legacy_unverified", report.resolve())
    ]
    assert "官方证据" in findings[0].reason
    assert report.read_bytes() == before_bytes
    assert hashlib.sha256(report.read_bytes()).hexdigest() == before_hash
    assert report.stat().st_mtime_ns == before_stat.st_mtime_ns
    assert video.read_bytes() == before_video


def test_structured_direct_evidence_is_not_misreported_as_legacy(tmp_path: Path) -> None:
    provenance = _provenance_module()
    report = (tmp_path / "direct" / "成片报告.json").resolve()
    report.parent.mkdir()
    report.write_text(
        json.dumps(
            {
                "素材匹配": [
                    {
                        "素材类型": "官方证据画面",
                        "evidence_kind": "direct_evidence",
                        "source_url": "https://example.com/news/1",
                        "is_locally_generated": False,
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    findings = provenance.audit_legacy_material_reports(report_files=[report])

    assert findings == []


def test_structured_explanation_with_spoofed_official_label_is_unverified(
    tmp_path: Path,
) -> None:
    provenance = _provenance_module()
    report = (tmp_path / "explanation" / "成片报告.json").resolve()
    report.parent.mkdir()
    report.write_text(
        json.dumps(
            {
                "素材匹配": [
                    {
                        "素材类型": "官方证据画面",
                        "evidence_kind": "explanation",
                        "source_url": "",
                        "is_locally_generated": True,
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    findings = provenance.audit_legacy_material_reports(report_files=[report])

    assert len(findings) == 1
    assert findings[0].status == "legacy_unverified"


def test_report_audit_returns_distinct_missing_unreadable_and_malformed_findings(
    tmp_path: Path,
) -> None:
    provenance = _provenance_module()
    missing = (tmp_path / "missing.json").resolve()
    unreadable = (tmp_path / "not-a-report-file").resolve()
    unreadable.mkdir()
    malformed = (tmp_path / "malformed.json").resolve()
    malformed.write_text("{not-json", encoding="utf-8")

    findings = provenance.audit_legacy_material_reports(
        report_files=[missing, unreadable, malformed]
    )

    by_path = {Path(item.path): item.status for item in findings}
    assert by_path == {
        missing: "missing",
        unreadable: "unreadable",
        malformed: "malformed",
    }


def test_report_audit_scans_only_explicit_absolute_roots_and_deduplicates(
    tmp_path: Path,
) -> None:
    provenance = _provenance_module()
    root = (tmp_path / "reports").resolve()
    nested = root / "run-1"
    nested.mkdir(parents=True)
    report = nested / "成片报告.json"
    report.write_text(
        '{"素材匹配":[{"素材类型":"官方证据画面"}]}',
        encoding="utf-8",
    )

    findings = provenance.audit_legacy_material_reports(
        report_files=[report],
        report_roots=[root, root],
    )

    assert len(findings) == 1
    assert findings[0].status == "legacy_unverified"
    assert Path(findings[0].path) == report.resolve()
    assert provenance.audit_legacy_material_reports() == []


def test_report_root_scans_finished_and_material_pipeline_report_names(
    tmp_path: Path,
) -> None:
    provenance = _provenance_module()
    root = (tmp_path / "reports").resolve()
    finished = root / "run-1" / "成片报告.json"
    material_pipeline = root / "run-2" / "素材链路报告.json"
    finished.parent.mkdir(parents=True)
    material_pipeline.parent.mkdir(parents=True)
    legacy_payload = '{"素材匹配":[{"素材类型":"官方证据画面"}]}'
    finished.write_text(legacy_payload, encoding="utf-8")
    material_pipeline.write_text(legacy_payload, encoding="utf-8")

    findings = provenance.audit_legacy_material_reports(report_roots=[root])

    assert {(Path(item.path), item.status) for item in findings} == {
        (finished, "legacy_unverified"),
        (material_pipeline, "legacy_unverified"),
    }


def test_invalid_or_incomplete_structured_provenance_is_legacy_unverified(
    tmp_path: Path,
) -> None:
    provenance = _provenance_module()
    invalid_rows = [
        {
            "素材类型": "来源页面录屏",
            "evidence_kind": "direct_evidence",
            "source_url": "",
            "is_locally_generated": False,
        },
        {
            "素材类型": "来源页面录屏",
            "evidence_kind": "direct_evidence",
            "source_url": "file:///D:/Temp/page.html",
            "is_locally_generated": False,
        },
        {
            "素材类型": "Pexels视频",
            "evidence_kind": "atmosphere",
            "source_url": "",
        },
        {
            "素材类型": "来源页面录屏",
            "source_url": "https://example.com/news/1",
        },
    ]
    reports: list[Path] = []
    for index, row in enumerate(invalid_rows, start=1):
        report = (tmp_path / f"invalid-{index}.json").resolve()
        report.write_text(
            json.dumps({"素材匹配": [row]}, ensure_ascii=False),
            encoding="utf-8",
        )
        reports.append(report)

    findings = provenance.audit_legacy_material_reports(report_files=reports)

    assert {Path(item.path) for item in findings} == set(reports)
    assert all(item.status == "legacy_unverified" for item in findings)


def test_valid_empty_failure_provenance_is_not_reported_as_legacy(
    tmp_path: Path,
) -> None:
    provenance = _provenance_module()
    report = (tmp_path / "failed-material.json").resolve()
    report.write_text(
        json.dumps(
            {
                "素材匹配": [
                    {
                        "素材状态": "失败",
                        "evidence_kind": "",
                        "source_url": "ftp://example.com/attempted",
                        "is_locally_generated": False,
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    assert provenance.audit_legacy_material_reports(report_files=[report]) == []


@pytest.mark.parametrize(
    ("status_key", "status_value"),
    [("素材状态", "已完成"), ("状态", "成功")],
)
def test_explicit_success_status_cannot_use_empty_failure_provenance(
    tmp_path: Path,
    status_key: str,
    status_value: str,
) -> None:
    provenance = _provenance_module()
    report = (tmp_path / f"success-empty-{status_key}.json").resolve()
    report.write_text(
        json.dumps(
            {
                "素材匹配": [
                    {
                        status_key: status_value,
                        "evidence_kind": "",
                        "source_url": "",
                        "is_locally_generated": False,
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    findings = provenance.audit_legacy_material_reports(report_files=[report])

    assert len(findings) == 1
    assert findings[0].status == "legacy_unverified"


@pytest.mark.parametrize(
    ("status_key", "status_value"),
    [("素材状态", "失败"), ("状态", "下载失败")],
)
def test_explicit_failure_status_cannot_use_success_provenance(
    tmp_path: Path,
    status_key: str,
    status_value: str,
) -> None:
    provenance = _provenance_module()
    report = (tmp_path / f"failure-success-{status_key}.json").resolve()
    report.write_text(
        json.dumps(
            {
                "素材匹配": [
                    {
                        status_key: status_value,
                        "evidence_kind": "atmosphere",
                        "source_url": "https://example.com/attempted.mp4",
                        "is_locally_generated": False,
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    findings = provenance.audit_legacy_material_reports(report_files=[report])

    assert len(findings) == 1
    assert findings[0].status == "legacy_unverified"


def test_ambiguous_explicit_status_with_provenance_is_legacy_unverified(
    tmp_path: Path,
) -> None:
    provenance = _provenance_module()
    report = (tmp_path / "ambiguous-status.json").resolve()
    report.write_text(
        json.dumps(
            {
                "素材匹配": [
                    {
                        "状态": "处理中",
                        "evidence_kind": "atmosphere",
                        "source_url": "",
                        "is_locally_generated": False,
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    findings = provenance.audit_legacy_material_reports(report_files=[report])

    assert len(findings) == 1
    assert findings[0].status == "legacy_unverified"


@pytest.mark.parametrize("outcome_key", ["success", "成功"])
def test_explicit_boolean_failure_cannot_use_success_provenance(
    tmp_path: Path,
    outcome_key: str,
) -> None:
    provenance = _provenance_module()
    report = (tmp_path / f"boolean-failure-{outcome_key}.json").resolve()
    report.write_text(
        json.dumps(
            {
                "素材匹配": [
                    {
                        outcome_key: False,
                        "evidence_kind": "atmosphere",
                        "source_url": "",
                        "is_locally_generated": False,
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    findings = provenance.audit_legacy_material_reports(report_files=[report])

    assert len(findings) == 1
    assert findings[0].status == "legacy_unverified"


def test_conflicting_text_and_boolean_outcomes_are_legacy_unverified(
    tmp_path: Path,
) -> None:
    provenance = _provenance_module()
    report = (tmp_path / "conflicting-outcomes.json").resolve()
    report.write_text(
        json.dumps(
            {
                "素材匹配": [
                    {
                        "状态": "已完成",
                        "success": False,
                        "evidence_kind": "atmosphere",
                        "source_url": "",
                        "is_locally_generated": False,
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    findings = provenance.audit_legacy_material_reports(report_files=[report])

    assert len(findings) == 1
    assert findings[0].status == "legacy_unverified"


def test_non_boolean_explicit_success_signal_is_legacy_unverified(
    tmp_path: Path,
) -> None:
    provenance = _provenance_module()
    report = (tmp_path / "non-boolean-success.json").resolve()
    report.write_text(
        json.dumps(
            {
                "素材匹配": [
                    {
                        "success": "true",
                        "evidence_kind": "atmosphere",
                        "source_url": "",
                        "is_locally_generated": False,
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    findings = provenance.audit_legacy_material_reports(report_files=[report])

    assert len(findings) == 1
    assert findings[0].status == "legacy_unverified"


def test_explicit_boolean_failure_accepts_empty_provenance(tmp_path: Path) -> None:
    provenance = _provenance_module()
    report = (tmp_path / "boolean-failure-empty.json").resolve()
    report.write_text(
        json.dumps(
            {
                "素材匹配": [
                    {
                        "success": False,
                        "evidence_kind": "",
                        "source_url": "https://example.com/attempted",
                        "is_locally_generated": False,
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    assert provenance.audit_legacy_material_reports(report_files=[report]) == []


def test_current_test_export_report_does_not_self_report_as_legacy(
    tmp_path: Path,
) -> None:
    provenance = _provenance_module()
    from app.materials.end_to_end_video import EndToEndVideoService

    service = EndToEndVideoService(
        output_root=tmp_path / "成片测试",
        material_root=tmp_path / "素材",
        enable_real_downloads=False,
        enable_real_render=False,
    )

    result = service.run_test_export(topic="AI 搜索正在改变普通人获取信息的方式")
    report = Path(result["导出文件"]["报告 JSON"])
    findings = provenance.audit_legacy_material_reports(report_files=[report])

    assert findings == []
    assert result["素材源结果"][0]["状态"] == "已生成"
    assert result["素材源结果"][0]["evidence_kind"] == "test_placeholder"


def test_current_smoke_material_and_operational_rows_audit_cleanly(
    tmp_path: Path,
) -> None:
    provenance = _provenance_module()
    from app.materials.run_material_pipeline_smoke import _build_smoke_report_row

    operational_rows = [
        _build_smoke_report_row(
            "目录检查",
            "成功",
            "临时素材目录可用。",
            str(tmp_path),
            include_provenance=False,
        ),
        _build_smoke_report_row(
            "自动成片",
            "失败",
            "最终渲染编排失败。",
            include_provenance=False,
        ),
    ]
    material_rows = [
        _build_smoke_report_row(
            "Pexels视频",
            "已下载",
            "已处理视频素材。",
            str(tmp_path / "pexels.mp4"),
            "atmosphere",
            "https://videos.pexels.com/example.mp4",
            False,
        ),
        _build_smoke_report_row("Pexels", "失败", "素材下载异常。"),
    ]
    report = (tmp_path / "素材链路报告.json").resolve()
    report.write_text(
        json.dumps(
            {"素材源结果": operational_rows + material_rows},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    findings = provenance.audit_legacy_material_reports(report_files=[report])

    assert findings == []
    assert all(
        not {"evidence_kind", "source_url", "is_locally_generated"}.intersection(row)
        for row in operational_rows
    )
    assert all(
        {"evidence_kind", "source_url", "is_locally_generated"} <= set(row)
        for row in material_rows
    )


def test_current_smoke_row_builder_does_not_hide_invalid_material_tuple(
    tmp_path: Path,
) -> None:
    provenance = _provenance_module()
    from app.materials.run_material_pipeline_smoke import _build_smoke_report_row

    invalid_row = _build_smoke_report_row(
        "Pexels",
        "失败",
        "下载失败却错误声明了成功素材。",
        evidence_kind="atmosphere",
        source_url="https://videos.pexels.com/attempted.mp4",
        is_locally_generated=False,
    )
    report = (tmp_path / "素材链路报告.json").resolve()
    report.write_text(
        json.dumps({"素材源结果": [invalid_row]}, ensure_ascii=False),
        encoding="utf-8",
    )

    findings = provenance.audit_legacy_material_reports(report_files=[report])

    assert len(findings) == 1
    assert findings[0].status == "legacy_unverified"


def test_explicit_report_inputs_reject_a_deterministic_reparse_ancestor(
    tmp_path: Path,
    monkeypatch,
) -> None:
    provenance = _provenance_module()
    ancestor = (tmp_path / "reparse-ancestor").resolve()
    root = ancestor / "reports"
    root.mkdir(parents=True)
    report = root / "成片报告.json"
    report.write_text(
        '{"素材匹配":[{"素材类型":"官方证据画面"}]}',
        encoding="utf-8",
    )
    checked: list[Path] = []

    def simulated_reparse(path: Path) -> bool:
        checked.append(Path(path))
        return Path(path) == ancestor

    monkeypatch.setattr(provenance, "_is_reparse_path", simulated_reparse)

    findings = provenance.audit_legacy_material_reports(
        report_files=[report],
        report_roots=[root],
    )

    assert ancestor in checked
    assert findings
    assert all(item.status == "unreadable" for item in findings)


def test_explicit_report_file_below_real_symlink_ancestor_is_not_opened(
    tmp_path: Path,
) -> None:
    provenance = _provenance_module()
    target = (tmp_path / "target").resolve()
    target.mkdir()
    target_report = target / "成片报告.json"
    target_report.write_text(
        '{"素材匹配":[{"素材类型":"官方证据画面"}]}',
        encoding="utf-8",
    )
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink privilege unavailable: {exc}")

    findings = provenance.audit_legacy_material_reports(
        report_files=[linked / "成片报告.json"]
    )

    assert findings
    assert all(item.status == "unreadable" for item in findings)


def test_audit_cli_without_report_root_keeps_exact_pre_task5_json_shape(
    tmp_path: Path,
) -> None:
    database = tmp_path / "audit.db"
    _create_database(database)

    result = _run_audit_cli(database)

    assert result.returncode == 0, result.stderr
    assert set(json.loads(result.stdout)) == DATABASE_AUDIT_KEYS


def test_audit_cli_discloses_legacy_report_without_changing_clean_database_exit(
    tmp_path: Path,
) -> None:
    database = tmp_path / "audit.db"
    _create_database(database)
    report_root = (tmp_path / "reports").resolve()
    report_root.mkdir()
    (report_root / "成片报告.json").write_text(
        '{"素材匹配":[{"素材类型":"官方证据画面"}]}',
        encoding="utf-8",
    )

    result = _run_audit_cli(database, "--report-root", str(report_root))

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert set(payload) == DATABASE_AUDIT_KEYS | {"legacy_report_findings"}
    assert payload["legacy_report_findings"][0]["status"] == "legacy_unverified"


def test_audit_cli_fails_requested_missing_or_malformed_report_audit(
    tmp_path: Path,
) -> None:
    database = tmp_path / "audit.db"
    _create_database(database)
    missing_root = (tmp_path / "missing-reports").resolve()
    malformed_root = (tmp_path / "malformed-reports").resolve()
    malformed_root.mkdir()
    (malformed_root / "成片报告.json").write_text("{bad", encoding="utf-8")

    missing_result = _run_audit_cli(
        database,
        "--report-root",
        str(missing_root),
    )
    malformed_result = _run_audit_cli(
        database,
        "--report-root",
        str(malformed_root),
    )

    assert missing_result.returncode == 2
    assert json.loads(missing_result.stdout)["legacy_report_findings"][0]["status"] == "missing"
    assert malformed_result.returncode == 2
    assert json.loads(malformed_result.stdout)["legacy_report_findings"][0]["status"] == "malformed"
