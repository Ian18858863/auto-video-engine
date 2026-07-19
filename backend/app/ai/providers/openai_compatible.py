from __future__ import annotations

import json

import httpx

from app.ai.schemas import AIRequest, AIResponse


class OpenAICompatibleProvider:
    name = "openai-compatible"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        provider_name: str = "",
        timeout: float = 60.0,
        max_retries: int = 2,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.name = provider_name or self.name
        self.timeout = timeout
        self.max_retries = max(1, max_retries)

    def generate(self, request: AIRequest) -> AIResponse:
        if not self.base_url or not self.api_key or not self.model:
            raise RuntimeError("真实 AI provider 未配置完整，不能发起调用。")

        messages = []
        if request.system_prompt:
            messages.append({"role": "system", "content": request.system_prompt})
        user_content = request.user_prompt or json.dumps(
            request.input_content,
            ensure_ascii=False,
            sort_keys=True,
        )
        messages.append({"role": "user", "content": user_content})

        last_error: Exception | None = None
        for _attempt in range(self.max_retries):
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    response = client.post(
                        f"{self.base_url}/chat/completions",
                        headers={"Authorization": f"Bearer {self.api_key}"},
                        json={
                            "model": self.model,
                            "messages": messages,
                            "temperature": 0.7,
                        },
                    )
                    response.raise_for_status()
                    data = response.json()
                break
            except Exception as exc:
                last_error = exc
        else:
            raise RuntimeError("真实 AI 不可用，已回退 fake provider。") from last_error

        content = data["choices"][0]["message"]["content"]
        return AIResponse(
            provider=self.name,
            model=self.model,
            output_content={
                "text": content,
                "generation_type": request.generation_type_value,
            },
        )
