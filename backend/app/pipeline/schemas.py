from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class BuildProjectRequest(BaseModel):
    project_name: str = Field(default="", max_length=120)
    selected_topic_id: int | None = None
    stop_after: str | None = None


class ContinuePipelineRequest(BaseModel):
    force: bool = False
    stop_after: str | None = None


class ScriptWorkspaceUpdateRequest(BaseModel):
    voiceover: str = Field(default="", max_length=20000)
    storyboard: list[dict[str, Any]] = Field(default_factory=list)
    subtitles: list[dict[str, Any]] = Field(default_factory=list)
    title_options: list[dict[str, Any]] = Field(default_factory=list)
    cover_copy: list[dict[str, Any]] = Field(default_factory=list)
    publish_copy: dict[str, Any] = Field(default_factory=dict)
    use_avatar: bool | None = None


class ScriptPresentationOptionsRequest(BaseModel):
    use_avatar: bool = True


class ScriptWorkOrderImportRequest(BaseModel):
    voiceover: str = Field(default="", max_length=20000)
    storyboard: list[dict[str, Any]] = Field(default_factory=list)
    subtitles: list[dict[str, Any]] = Field(default_factory=list)
    animation_prompts: list[dict[str, Any]] = Field(default_factory=list)
    title_options: list[dict[str, Any]] = Field(default_factory=list)
    cover_copy: list[dict[str, Any]] = Field(default_factory=list)
    publish_copy: dict[str, Any] = Field(default_factory=dict)
    raw_result: str = Field(default="", max_length=20000)


class ScriptVersionRestoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    source_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_snapshot_hash: str = Field(pattern=r"^(?:[0-9a-f]{64})?$")
    expected_current_version_id: str = Field(min_length=1, max_length=160)
    expected_current_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_current_snapshot_hash: str = Field(
        pattern=r"^(?:[0-9a-f]{64})?$"
    )
    expected_script_state_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class PipelineVoiceoverRequest(BaseModel):
    provider: str | None = None


QualityEvidenceText = Annotated[str, Field(min_length=1, max_length=2_000)]
QualityReviewText = Annotated[str, Field(min_length=1, max_length=1_000)]
QualityOptionalReviewText = Annotated[str, Field(max_length=1_000)]


class QualityManualGateRequest(BaseModel):
    """One bounded manual attestation submitted with a quality report."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["passed", "failed"]
    evidence: list[QualityEvidenceText] = Field(min_length=1, max_length=32)

    @field_validator("evidence", mode="before")
    @classmethod
    def normalize_evidence(cls, value: object) -> object:
        # Keep the API compatible with the first internal writer, which also
        # accepted one evidence string, while storing one canonical list shape.
        return [value] if isinstance(value, str) else value


class QualityMustPreserveResultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result: Literal["passed", "failed"]
    evidence: QualityReviewText


class QualityAllowedDifferenceResultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result: Literal["used", "not_used"]
    actual_difference: QualityOptionalReviewText
    evidence: QualityReviewText


class QualityPlannedExceedResultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result: Literal["exceeded", "matched", "not_met", "not_comparable"]
    difference: QualityReviewText
    evidence: QualityReviewText


class QualityContractReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    must_preserve_results: dict[str, QualityMustPreserveResultRequest] = Field(
        min_length=1,
        max_length=16,
    )
    allowed_difference_results: dict[
        str, QualityAllowedDifferenceResultRequest
    ] = Field(min_length=1, max_length=16)
    planned_exceed_results: dict[str, QualityPlannedExceedResultRequest] = Field(
        min_length=1,
        max_length=16,
    )


class QualityReportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_relative_path: str = Field(min_length=1, max_length=400)
    gates: dict[str, QualityManualGateRequest] | None = None
    quality_contract_review: QualityContractReviewRequest | None = None

    @field_validator("gates")
    @classmethod
    def validate_gate_names(
        cls, value: dict[str, QualityManualGateRequest] | None
    ) -> dict[str, QualityManualGateRequest] | None:
        if value is not None and not set(value).issubset(
            {"visual_narration", "facts_rights"}
        ):
            raise ValueError("unknown quality gate")
        return value


class QualityApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_relative_path: str = Field(min_length=1, max_length=400)
    quality_report_relative_path: str = Field(min_length=1, max_length=400)
    approved_by: str = Field(min_length=1, max_length=40)
    approved_at: str | None = Field(default=None, max_length=80)


class ManualV1RunRequest(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    summary: str = Field(default="", max_length=300)
    source_platform: str = Field(default="手动跑通测试", max_length=100)
    url: str = Field(default="", max_length=800)
    category: str = Field(default="AI / 科技趋势 / 商业逻辑", max_length=80)
    tags: list[str] = Field(default_factory=list, max_length=5)
    notes: str = Field(default="", max_length=500)
