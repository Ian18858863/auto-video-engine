from __future__ import annotations

from typing import Protocol

from app.ai.schemas import AIRequest, AIResponse


class AIProvider(Protocol):
    name: str
    model: str

    def generate(self, request: AIRequest) -> AIResponse:
        raise NotImplementedError
