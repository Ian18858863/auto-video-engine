from __future__ import annotations

import hashlib
import json
from typing import Any

from app.ai.schemas import AIRequest


def build_input_hash(request: AIRequest, provider: str, model: str) -> str:
    payload: dict[str, Any] = {
        "generation_type": request.generation_type_value,
        "input_content": request.input_content,
        "system_prompt": request.system_prompt,
        "user_prompt": request.user_prompt,
        "provider": provider,
        "model": model,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_input_snapshot(request: AIRequest) -> dict[str, Any]:
    return {
        "generation_type": request.generation_type_value,
        "input_content": request.input_content,
        "system_prompt": request.system_prompt,
        "user_prompt": request.user_prompt,
    }
