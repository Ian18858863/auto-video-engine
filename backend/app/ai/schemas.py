from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class GenerationType(StrEnum):
    TOPIC_IDEAS = "topic_ideas"
    SCRIPT = "script"
    REWRITE_HOOK = "rewrite_hook"
    REWRITE_ENDING = "rewrite_ending"
    CONTENT_QA = "content_qa"
    BREAKDOWN_REPORT = "breakdown_report"
    REPRODUCTION_SOP = "reproduction_sop"
    VIRAL_REVIEW = "viral_review"
    COMMENT_INSIGHTS = "comment_insights"
    COPY_STYLE_TEST = "copy_style_test"


@dataclass(slots=True)
class AIRequest:
    generation_type: GenerationType | str
    input_content: dict[str, Any]
    system_prompt: str = ""
    user_prompt: str = ""

    @property
    def generation_type_value(self) -> str:
        if isinstance(self.generation_type, GenerationType):
            return self.generation_type.value
        return str(self.generation_type)


@dataclass(slots=True)
class AIResponse:
    output_content: dict[str, Any]
    provider: str
    model: str


@dataclass(slots=True)
class CachedAIResponse(AIResponse):
    input_hash: str
    from_cache: bool


@dataclass(slots=True)
class AIProviderConfig:
    provider: str = "fake"
    base_url: str = ""
    api_key: str = ""
    model: str = "fake-low-cost"
    timeout: float = 60.0
    max_retries: int = 2
    extra: dict[str, Any] = field(default_factory=dict)
