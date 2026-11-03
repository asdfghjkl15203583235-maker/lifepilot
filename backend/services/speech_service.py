"""Speech-to-text (SiliconFlow) and text-to-speech (edge-tts)."""

import logging

import edge_tts
from openai import AsyncOpenAI, AuthenticationError

from config import Settings

logger = logging.getLogger(__name__)

SILICONFLOW_BASE_URL = "https://api.siliconflow.cn/v1"
SILICONFLOW_TRANSCRIPTION_MODEL = "FunAudioLLM/SenseVoiceSmall"

SILICONFLOW_TRANSCRIBE_TIMEOUT_SECONDS = 120.0

SYNTHESIZED_AUDIO_MIME_TYPE = "audio/mpeg"

SILICONFLOW_INVALID_API_KEY_USER_MESSAGE = (
    "硅基流动拒绝了该 API 密钥。请确认 backend/.env 中的 SILICONFLOW_API_KEY 与 "
    "https://cloud.siliconflow.cn 一致（不要带引号或空格）。"
    "如果你在当前终端里执行过 export SILICONFLOW_API_KEY，请先 unset 它，因为环境变量会覆盖 .env。"
    "修改环境变量后请重启 uvicorn。"
)

SILICONFLOW_NETWORK_USER_MESSAGE = (
    "语音转写失败：连接硅基流动时出现网络错误。"
    "请重试一次；如果仍然失败，请为 api.siliconflow.cn 关闭 VPN/代理，清除异常的 "
    "HTTP_PROXY/HTTPS_PROXY 环境变量，或在 Python 3.11–3.13 上运行后端（3.14 的 TLS 可能不稳定）。"
)


class SiliconFlowTranscriptionRequestError(RuntimeError):
    """SiliconFlow speech API rejected the transcription request (non-auth failure)."""


class SiliconFlowInvalidApiKeyError(RuntimeError):
    """SiliconFlow returned 401 — API key missing, revoked, or typo."""


async def transcribe_audio(
    settings: Settings,
    audio_bytes: bytes,
    filename: str,
) -> str:
    """Transcribe audio bytes using SiliconFlow OpenAI-compatible speech API."""
    if not settings.siliconflow_api_key:
        raise ValueError("SILICONFLOW_API_KEY 尚未配置")

    client = AsyncOpenAI(
        api_key=settings.siliconflow_api_key,
        base_url=settings.siliconflow_base_url,
        timeout=SILICONFLOW_TRANSCRIBE_TIMEOUT_SECONDS,
    )
    try:
        result = await client.audio.transcriptions.create(
            model=settings.siliconflow_transcription_model,
            file=(filename or "recording.webm", audio_bytes),
        )
    except AuthenticationError as exc:
        logger.error("SiliconFlow authentication failed: %s", exc)
        raise SiliconFlowInvalidApiKeyError(SILICONFLOW_INVALID_API_KEY_USER_MESSAGE) from exc
    except Exception as exc:
        logger.error("SiliconFlow transcription failed: %s", exc)
        raise SiliconFlowTranscriptionRequestError(
            "语音转写失败。请查看服务器日志。",
        ) from exc

    text = getattr(result, "text", None)
    if isinstance(text, str) and text.strip():
        return text.strip()

    logger.error("SiliconFlow transcription returned unexpected payload: %r", result)
    raise SiliconFlowTranscriptionRequestError(
        "语音转写没有返回文本。请重试，或使用更清晰的录音。",
    )


async def synthesize_speech(settings: Settings, text: str) -> bytes:
    """Synthesize speech as MPEG audio using edge-tts."""
    stripped = text.strip()
    if not stripped:
        raise ValueError("无法合成空文本")

    communicate = edge_tts.Communicate(stripped, settings.edge_tts_voice)
    chunks: list[bytes] = []
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            chunks.append(chunk["data"])

    return b"".join(chunks)


def speech_audio_mime_type() -> str:
    """MIME type returned by edge-tts streaming synthesis."""
    return SYNTHESIZED_AUDIO_MIME_TYPE
