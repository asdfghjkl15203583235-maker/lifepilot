import uuid
from collections.abc import AsyncGenerator

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import get_current_user_id
from config import Settings, get_settings
from db.session import get_session
from schemas.chat import ChatCancelRequest, ChatCompletionRequest
from services.agent_runner import cancel_conversation_turn, stream_chat_agent

chat_router = APIRouter(tags=["chat"])


@chat_router.post("/chat")
async def post_chat_completion(
    body: ChatCompletionRequest,
    background_tasks: BackgroundTasks,
    settings: Settings = Depends(get_settings),
    user_id: uuid.UUID = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    if not settings.openrouter_api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="OPENROUTER_API_KEY 尚未配置",
        )

    payloads = [m.model_dump() for m in body.messages]

    async def event_stream() -> AsyncGenerator[str, None]:
        async for chunk in stream_chat_agent(
            settings=settings,
            session=session,
            user_id=user_id,
            message_payloads=payloads,
            model=body.model,
            client_conversation_id=body.client_conversation_id,
            last_event_id=last_event_id,
        ):
            yield chunk

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@chat_router.post("/chat/cancel")
async def cancel_chat_turn(
    body: ChatCancelRequest,
    user_id: uuid.UUID = Depends(get_current_user_id),
) -> dict[str, str]:
    """用户主动停止：立刻取消这一轮 LangGraph 执行并释放资源。

    客户端 `abort()` 只能断开连接，后端无法区分"用户点停止"和"网络抖动"；
    这个接口把意图显式传过来，避免把正常的断线重连也当成取消。
    """
    is_cancelled = cancel_conversation_turn(body.client_conversation_id)
    return {"status": "cancelled" if is_cancelled else "idle"}
