from pydantic import BaseModel, Field


class VoiceCompletionResponse(BaseModel):
    """Assistant reply plus transcription and synthesized speech from POST /voice."""

    transcript: str = Field(description="Speech-to-text of the uploaded audio")
    content: str = Field(description="Assistant reply text")
    audio_base64: str = Field(description="Base64-encoded synthesized speech (MPEG)")
    audio_mime_type: str = Field(
        default="audio/mpeg",
        description="MIME type of audio_base64 payload",
    )
