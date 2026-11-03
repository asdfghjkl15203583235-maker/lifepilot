"""Orchestrate transcription, agent reply, and speech synthesis for voice chat."""

import base64
import uuid

from fastapi import BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession

from config import Settings
from schemas.chat import ChatMessage
from schemas.voice import VoiceCompletionResponse
from services.agent_runner import run_chat_agent
from services.speech_service import speech_audio_mime_type, synthesize_speech, transcribe_audio


class EmptyTranscriptError(Exception):
    """Raised when the transcription model returns no usable user text."""


async def complete_voice_turn(
    *,
    settings: Settings,
    session: AsyncSession,
    user_id: uuid.UUID,
    prior_messages: list[ChatMessage],
    audio_bytes: bytes,
    audio_filename: str,
    completion_model: str | None,
    client_conversation_id: str | None = None,
    background_tasks: BackgroundTasks | None = None,
) -> VoiceCompletionResponse:
    """Transcribe audio, run the agent, and return reply with synthesized speech."""
    transcript = await transcribe_audio(settings, audio_bytes, audio_filename)

    if not transcript.strip():
        raise EmptyTranscriptError()

    combined_payloads = [m.model_dump() for m in prior_messages]
    combined_payloads.append({"role": "user", "content": transcript})

    final_answer = await run_chat_agent(
        settings=settings,
        session=session,
        user_id=user_id,
        message_payloads=combined_payloads,
        model=completion_model,
        is_voice_request=True,
        client_conversation_id=client_conversation_id,
        background_tasks=background_tasks,
    )

    audio_bytes_out = await synthesize_speech(settings, final_answer)
    encoded = base64.standard_b64encode(audio_bytes_out).decode("ascii")

    return VoiceCompletionResponse(
        transcript=transcript,
        content=final_answer,
        audio_base64=encoded,
        audio_mime_type=speech_audio_mime_type(),
    )
