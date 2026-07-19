from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.core.config import get_settings


class TTSError(RuntimeError):
    pass


@dataclass(slots=True)
class TTSResult:
    success: bool
    status: str
    audio_path: str = ""
    provider: str = ""
    reason: str = ""


def synthesize_tts(
    text: str,
    output_path: Path,
    *,
    provider: str | None = None,
) -> TTSResult:
    settings = get_settings()
    provider_name = provider or settings.tts_provider
    if provider_name == "windows_sapi":
        return WindowsSapiTTSProvider().synthesize(text, output_path)
    if provider_name in {"volcengine", "huoshan"}:
        return TTSResult(
            success=False,
            status="失败",
            provider=provider_name,
            reason="火山 TTS 接口已预留，但当前未配置，先使用免费 TTS 方案。",
        )
    return TTSResult(
        success=False,
        status="失败",
        provider=provider_name,
        reason=f"不支持的 TTS provider：{provider_name}",
    )


class WindowsSapiTTSProvider:
    provider_name = "windows_sapi"

    def synthesize(self, text: str, output_path: Path) -> TTSResult:
        text = text.strip()
        if not text:
            return TTSResult(
                success=False,
                status="失败",
                provider=self.provider_name,
                reason="口播文本为空，无法生成配音。",
            )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        settings = get_settings()
        script = _powershell_tts_script(
            text=text,
            output_path=output_path,
            voice_name=settings.tts_voice_name,
            rate=settings.tts_rate,
            volume=settings.tts_volume,
        )
        try:
            completed = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    script,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=120,
                check=False,
            )
        except Exception as exc:
            return TTSResult(
                success=False,
                status="失败",
                provider=self.provider_name,
                reason=f"Windows 免费 TTS 执行失败：{exc}",
            )
        if completed.returncode != 0 or not output_path.is_file():
            error = (completed.stderr or completed.stdout or "未知错误").strip()
            return TTSResult(
                success=False,
                status="失败",
                provider=self.provider_name,
                reason=f"Windows 免费 TTS 生成失败：{error}",
            )
        return TTSResult(
            success=True,
            status="已生成",
            audio_path=str(output_path),
            provider=self.provider_name,
            reason="已使用 Windows 免费 TTS 生成口播音频。",
        )


def _powershell_tts_script(
    *,
    text: str,
    output_path: Path,
    voice_name: str,
    rate: int,
    volume: int,
) -> str:
    safe_text = json.dumps(text, ensure_ascii=False)
    safe_path = json.dumps(str(output_path), ensure_ascii=False)
    safe_voice = json.dumps(voice_name, ensure_ascii=False)
    safe_rate = max(-10, min(10, int(rate)))
    safe_volume = max(0, min(100, int(volume)))
    return (
        "Add-Type -AssemblyName System.Speech; "
        "$speak = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        f"$voice = {safe_voice}; "
        "if ($voice -ne '') { try { $speak.SelectVoice($voice) } catch {} }; "
        f"$speak.Rate = {safe_rate}; "
        f"$speak.Volume = {safe_volume}; "
        f"$path = {safe_path}; "
        "$speak.SetOutputToWaveFile($path); "
        f"$speak.Speak({safe_text}); "
        "$speak.Dispose(); "
        "Write-Output 'TTS_OK'"
    )
