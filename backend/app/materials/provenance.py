from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence
from urllib.parse import urlparse


SUCCESS_EVIDENCE_KINDS = frozenset(
    {
        "direct_evidence",
        "explanation",
        "atmosphere",
        "test_placeholder",
    }
)
_REPARSE_POINT_ATTRIBUTE = 0x400
_PROVENANCE_KEYS = frozenset(
    {"evidence_kind", "source_url", "is_locally_generated"}
)
_REPORT_FILENAMES = frozenset({"成片报告.json", "素材链路报告.json"})
_STATUS_KEYS = ("素材状态", "状态", "material_status", "status")
_OUTCOME_KEYS = ("success", "成功")
_SUCCESS_STATUSES = frozenset(
    {
        "成功",
        "完成",
        "已完成",
        "已下载",
        "部分下载",
        "已生成",
        "已匹配",
        "本地已匹配",
        "success",
        "succeeded",
        "complete",
        "completed",
        "downloaded",
        "generated",
        "matched",
        "partial_success",
    }
)
_FAILURE_STATUSES = frozenset(
    {
        "失败",
        "下载失败",
        "生成失败",
        "缺素材",
        "未开始",
        "未下载",
        "未生成",
        "无结果",
        "未配置",
        "已跳过",
        "已跳过真实下载",
        "跳过",
        "筛选不通过",
        "取消",
        "已取消",
        "failed",
        "failure",
        "error",
        "missing",
        "not_started",
        "skipped",
        "disabled",
        "cancelled",
        "canceled",
    }
)


@dataclass(frozen=True, slots=True)
class MaterialProvenance:
    evidence_kind: str
    source_url: str
    is_locally_generated: bool

    def __post_init__(self) -> None:
        validate_success_provenance(
            evidence_kind=self.evidence_kind,
            source_url=self.source_url,
            is_locally_generated=self.is_locally_generated,
        )


@dataclass(frozen=True, slots=True)
class LegacyReportFinding:
    status: str
    path: str
    reason: str


def is_http_source_url(value: str) -> bool:
    parsed = urlparse(str(value or "").strip())
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)


def classify_capture_provenance(
    source_url: str,
    *,
    used_placeholder: bool,
) -> MaterialProvenance:
    normalized_url = str(source_url or "").strip()
    if used_placeholder or not is_http_source_url(normalized_url):
        return MaterialProvenance(
            evidence_kind="explanation",
            source_url=normalized_url if is_http_source_url(normalized_url) else "",
            is_locally_generated=True,
        )
    return MaterialProvenance(
        evidence_kind="direct_evidence",
        source_url=normalized_url,
        is_locally_generated=False,
    )


def classify_legacy_material_provenance(
    material_type: str,
    source_type: str,
) -> MaterialProvenance:
    """Conservatively classify labels that predate structured provenance.

    ``evidence_kind`` records the editorial role of an asset, while
    ``is_locally_generated`` independently records its origin.  An old
    "official" label therefore cannot become direct evidence without an HTTP
    source URL; it remains an explanation with no claim that it was generated
    locally.
    """
    label = f"{material_type} {source_type}".casefold()
    if "测试占位" in label or "test_placeholder" in label:
        return MaterialProvenance(
            evidence_kind="test_placeholder",
            source_url="",
            is_locally_generated=True,
        )
    if any(token in label for token in ("信息动效", "自动生成", "解释卡")):
        return MaterialProvenance(
            evidence_kind="explanation",
            source_url="",
            is_locally_generated=True,
        )
    if any(token in label for token in ("官方证据", "官方页面", "官方录屏")):
        return MaterialProvenance(
            evidence_kind="explanation",
            source_url="",
            is_locally_generated=False,
        )
    return MaterialProvenance(
        evidence_kind="atmosphere",
        source_url="",
        is_locally_generated=False,
    )


def validate_success_provenance(
    *,
    evidence_kind: str,
    source_url: str,
    is_locally_generated: bool,
) -> None:
    if evidence_kind not in SUCCESS_EVIDENCE_KINDS:
        raise ValueError(
            "successful material requires evidence_kind in "
            + ", ".join(sorted(SUCCESS_EVIDENCE_KINDS))
        )
    normalized_url = str(source_url or "").strip()
    if normalized_url and not is_http_source_url(normalized_url):
        raise ValueError("source_url must be an absolute http/https URL or empty")
    if evidence_kind == "direct_evidence":
        if not is_http_source_url(normalized_url):
            raise ValueError("direct_evidence requires an absolute http/https source_url")
        if is_locally_generated:
            raise ValueError("direct_evidence cannot be locally generated")
    if evidence_kind == "test_placeholder" and not is_locally_generated:
        raise ValueError("test_placeholder must be locally generated")


def validate_execution_provenance(
    *,
    success: bool,
    evidence_kind: str,
    source_url: str,
    is_locally_generated: bool,
) -> None:
    if success:
        validate_success_provenance(
            evidence_kind=evidence_kind,
            source_url=source_url,
            is_locally_generated=is_locally_generated,
        )
        return
    if evidence_kind:
        raise ValueError("failed material execution must use an empty evidence_kind")
    if is_locally_generated:
        raise ValueError("failed material execution cannot claim a generated asset")
    normalized_url = str(source_url or "").strip()
    if normalized_url and not is_http_source_url(normalized_url):
        # Failures may retain an unsupported attempted source for diagnosis.
        return


def audit_legacy_material_reports(
    *,
    report_files: Sequence[Path] = (),
    report_roots: Sequence[Path] = (),
) -> list[LegacyReportFinding]:
    report_paths: set[Path] = set()
    findings: list[LegacyReportFinding] = []
    for value in report_files:
        path = Path(value)
        if not path.is_absolute():
            findings.append(
                LegacyReportFinding(
                    status="unreadable",
                    path=str(path),
                    reason="报告文件必须使用显式绝对路径。",
                )
            )
            continue
        report_paths.add(path)

    for value in report_roots:
        root = Path(value)
        root_findings, root_reports = _reports_from_explicit_root(root)
        findings.extend(root_findings)
        report_paths.update(root_reports)

    for path in sorted(report_paths, key=_path_sort_key):
        finding = _audit_report_file(path)
        if finding is not None:
            findings.append(finding)
    return _deduplicated_findings(findings)


def _reports_from_explicit_root(
    root: Path,
) -> tuple[list[LegacyReportFinding], set[Path]]:
    if not root.is_absolute():
        return (
            [
                LegacyReportFinding(
                    status="unreadable",
                    path=str(root),
                    reason="报告目录必须使用显式绝对路径。",
                )
            ],
            set(),
        )
    reparse_component = _first_reparse_component(root)
    if reparse_component is not None:
        return (
            [
                LegacyReportFinding(
                    status="unreadable",
                    path=str(root),
                    reason=(
                        "显式报告目录位于链接或重解析路径之下："
                        f"{reparse_component}。"
                    ),
                )
            ],
            set(),
        )
    if not root.exists():
        return (
            [
                LegacyReportFinding(
                    status="missing",
                    path=str(root),
                    reason="显式报告目录不存在。",
                )
            ],
            set(),
        )
    if not root.is_dir():
        return (
            [
                LegacyReportFinding(
                    status="unreadable",
                    path=str(root),
                    reason="显式报告目录不是可安全遍历的普通目录。",
                )
            ],
            set(),
        )

    reports: set[Path] = set()
    findings: list[LegacyReportFinding] = []
    pending = [root]
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as entries:
                ordered_entries = sorted(entries, key=lambda item: item.name.casefold())
        except OSError as exc:
            findings.append(
                LegacyReportFinding(
                    status="unreadable",
                    path=str(current),
                    reason=f"报告目录无法读取：{type(exc).__name__}。",
                )
            )
            continue
        for entry in ordered_entries:
            entry_path = Path(entry.path)
            try:
                if entry.is_symlink() or _is_reparse_path(entry_path):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    pending.append(entry_path)
                elif (
                    entry.name in _REPORT_FILENAMES
                    and entry.is_file(follow_symlinks=False)
                ):
                    reports.add(entry_path)
            except OSError as exc:
                findings.append(
                    LegacyReportFinding(
                        status="unreadable",
                        path=str(entry_path),
                        reason=f"报告路径无法检查：{type(exc).__name__}。",
                    )
                )
    return findings, reports


def _audit_report_file(path: Path) -> LegacyReportFinding | None:
    reparse_component = _first_reparse_component(path)
    if reparse_component is not None:
        return LegacyReportFinding(
            status="unreadable",
            path=str(path),
            reason=(
                "显式报告文件位于链接或重解析路径之下："
                f"{reparse_component}。"
            ),
        )
    if not path.exists():
        return LegacyReportFinding(
            status="missing",
            path=str(path),
            reason="显式报告文件不存在。",
        )
    if not path.is_file():
        return LegacyReportFinding(
            status="unreadable",
            path=str(path),
            reason="显式报告路径不是可安全读取的普通文件。",
        )
    try:
        with path.open("r", encoding="utf-8-sig") as file:
            payload = json.load(file)
    except (UnicodeError, json.JSONDecodeError) as exc:
        return LegacyReportFinding(
            status="malformed",
            path=str(path),
            reason=f"报告不是有效 UTF-8 JSON：{type(exc).__name__}。",
        )
    except OSError as exc:
        return LegacyReportFinding(
            status="unreadable",
            path=str(path),
            reason=f"报告无法读取：{type(exc).__name__}。",
        )
    if not isinstance(payload, (dict, list)):
        return LegacyReportFinding(
            status="malformed",
            path=str(path),
            reason="报告 JSON 顶层必须是对象或数组。",
        )
    if _contains_fake_official_semantics(payload):
        return LegacyReportFinding(
            status="legacy_unverified",
            path=str(path),
            reason=(
                "报告含无效的结构化 provenance 三元组，或无法由结构化 "
                "provenance 证实的“官方证据”标签。"
            ),
        )
    return None


def _contains_fake_official_semantics(value: object) -> bool:
    if isinstance(value, str):
        return "官方证据" in value
    if isinstance(value, dict):
        if _has_invalid_structured_provenance(value):
            return True
        if _is_valid_direct_evidence_row(value):
            return any(
                _contains_fake_official_semantics(item)
                for item in value.values()
                if isinstance(item, (dict, list))
            )
        return any(
            _contains_fake_official_semantics(key)
            or _contains_fake_official_semantics(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_fake_official_semantics(item) for item in value)
    return False


def _has_invalid_structured_provenance(value: dict[object, object]) -> bool:
    present_keys = _PROVENANCE_KEYS.intersection(value)
    if not present_keys:
        return False
    if present_keys != _PROVENANCE_KEYS:
        return True

    evidence_kind = value.get("evidence_kind")
    source_url = value.get("source_url")
    is_locally_generated = value.get("is_locally_generated")
    if not isinstance(evidence_kind, str):
        return True
    if not isinstance(source_url, str):
        return True
    if not isinstance(is_locally_generated, bool):
        return True
    explicit_status = _classify_explicit_status(value)
    if explicit_status == "ambiguous":
        return True
    success = (
        bool(evidence_kind)
        if explicit_status == "absent"
        else explicit_status == "success"
    )
    try:
        validate_execution_provenance(
            success=success,
            evidence_kind=evidence_kind,
            source_url=source_url,
            is_locally_generated=is_locally_generated,
        )
    except ValueError:
        return True
    return False


def _classify_explicit_status(value: dict[object, object]) -> str:
    classifications: set[str] = set()
    found = False
    for key in _STATUS_KEYS:
        if key not in value:
            continue
        found = True
        raw_status = value.get(key)
        if not isinstance(raw_status, str):
            return "ambiguous"
        normalized = raw_status.strip().casefold().replace("-", "_").replace(" ", "_")
        if normalized in _SUCCESS_STATUSES:
            classifications.add("success")
        elif normalized in _FAILURE_STATUSES:
            classifications.add("failure")
        else:
            return "ambiguous"
    for key in _OUTCOME_KEYS:
        if key not in value:
            continue
        found = True
        outcome = value.get(key)
        if type(outcome) is not bool:
            return "ambiguous"
        classifications.add("success" if outcome else "failure")
    if not found:
        return "absent"
    if len(classifications) != 1:
        return "ambiguous"
    return next(iter(classifications))


def _is_valid_direct_evidence_row(value: dict[object, object]) -> bool:
    if value.get("evidence_kind") != "direct_evidence":
        return False
    if value.get("is_locally_generated") is not False:
        return False
    source_url = value.get("source_url")
    if not isinstance(source_url, str):
        return False
    try:
        validate_success_provenance(
            evidence_kind="direct_evidence",
            source_url=source_url,
            is_locally_generated=False,
        )
    except ValueError:
        return False
    return True


def _is_reparse_path(path: Path) -> bool:
    try:
        stat_result = os.lstat(path)
    except OSError:
        return False
    return path.is_symlink() or bool(
        getattr(stat_result, "st_file_attributes", 0) & _REPARSE_POINT_ATTRIBUTE
    )


def _first_reparse_component(path: Path) -> Path | None:
    # Deliberately inspect the lexical path.  Resolving it first would hide the
    # symlink/junction component that the audit must reject.
    for component in (path, *path.parents):
        if _is_reparse_path(component):
            return component
    return None


def _path_sort_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))


def _deduplicated_findings(
    findings: Iterable[LegacyReportFinding],
) -> list[LegacyReportFinding]:
    by_key: dict[tuple[str, str], LegacyReportFinding] = {}
    for finding in findings:
        key = (os.path.normcase(os.path.abspath(finding.path)), finding.status)
        by_key.setdefault(key, finding)
    return sorted(
        by_key.values(),
        key=lambda item: (os.path.normcase(os.path.abspath(item.path)), item.status),
    )
