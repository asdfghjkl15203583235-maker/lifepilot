import json
import logging
import uuid

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    UploadFile,
    status,
)
from pydantic import TypeAdapter, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import get_current_user_id
from config import Settings, get_settings
from db.session import get_session
from schemas.chat import ChatMessage
from schemas.voice import VoiceCompletionResponse
from services.speech_service import SiliconFlowInvalidApiKeyError, SiliconFlowTranscriptionRequestError
from services.voice_completion_service import EmptyTranscriptError, complete_voice_turn

logger = logging.getLogger(__name__)

voice_router = APIRouter(tags=["voice"])

PRIOR_MESSAGES_JSON_EMPTY = "[]"

PRIOR_MESSAGES_ADAPTER = TypeAdapter(list[ChatMessage])

EMPTY_AUDIO_DETAIL = "音频文件为空"

NO_SPEECH_DETAIL = "录音中未检测到语音"


@voice_router.post("/voice", response_model=VoiceCompletionResponse)
async def post_voice_completion(
    background_tasks: BackgroundTasks,
    audio: UploadFile = File(...),
    messages_json: str = Form(PRIOR_MESSAGES_JSON_EMPTY),
    completion_model: str | None = Form(None),
    client_conversation_id: str | None = Form(None),
    settings: Settings = Depends(get_settings),
    user_id: uuid.UUID = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
) -> VoiceCompletionResponse:
    if not settings.siliconflow_api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="SILICONFLOW_API_KEY 尚未配置",
        )
    if not settings.openrouter_api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="OPENROUTER_API_KEY 尚未配置",
        )

    try:
        parsed_messages = json.loads(messages_json)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="messages_json 必须是有效的 JSON",
        ) from exc

    try:
        prior_messages = PRIOR_MESSAGES_ADAPTER.validate_python(parsed_messages)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="messages_json 必须是聊天消息列表",
        ) from exc

    raw_audio = await audio.read()
    if len(raw_audio) == 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=EMPTY_AUDIO_DETAIL)

    filename = audio.filename or "recording.webm"

    try:
        return await complete_voice_turn(
            settings=settings,
            session=session,
            user_id=user_id,
            prior_messages=prior_messages,
            audio_bytes=raw_audio,
            audio_filename=filename,
            completion_model=completion_model,
            client_conversation_id=client_conversation_id,
            background_tasks=background_tasks,
        )
    except SiliconFlowInvalidApiKeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except SiliconFlowTranscriptionRequestError as exc:
        logger.warning("Transcription failed for user %s: %s", user_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except EmptyTranscriptError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=NO_SPEECH_DETAIL,
        ) from exc
    except Exception as exc:
        logger.exception("Voice completion failed for user %s", user_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="语音合成失败",
        ) from exc
