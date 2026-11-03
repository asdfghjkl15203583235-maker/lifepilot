from typing import Literal

from pydantic import BaseModel, Field

ChatMessageRole = Literal["system", "user", "assistant"]


class ChatMessage(BaseModel):
    role: ChatMessageRole
    content: str = Field(min_length=1)
    timestamp: str | None = Field(
        default=None,
        description="ISO-8601 timestamp of when the message was created (used to isolate "
        "the latest request from older history when composing LLM context).",
    )


class ChatCompletionRequest(BaseModel):
    """Body for POST /chat — OpenAI-compatible chat messages."""

    messages: list[ChatMessage] = Field(min_length=1)
    model: str | None = Field(
        default=None,
        description="OpenRouter model id; falls back to server default when omitted",
    )
    client_conversation_id: str | None = Field(
        default=None,
        description="Optional id from the client (e.g. tab id) for logs and parallel agent tracing",
    )


class ChatCancelRequest(BaseModel):
    """Body for POST /chat/cancel — 用户主动停止这一轮生成。"""

    client_conversation_id: str = Field(
        min_length=1,
        description="Conversation scope whose in-flight turn should be cancelled",
    )


class ChatCompleteResponse(BaseModel):
    """Assistant reply from POST /chat."""

    content: str = Field(description="Assistant message body")
