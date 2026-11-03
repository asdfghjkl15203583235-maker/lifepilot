"""VoyageAI embedding generation — thin wrapper over the voyageai SDK."""

import asyncio
import logging

import aiohttp
import voyageai
from voyageai import error as voyage_error

from config import Settings

logger = logging.getLogger(__name__)

_client: voyageai.AsyncClient | None = None

# Free-tier accounts are limited to 3 RPM; a single chat turn can embed several
# texts (memory injection + similarity search + write), so back off and retry
# instead of silently dropping the memory.
_RATE_LIMIT_RETRIES = 2
_RATE_LIMIT_BACKOFF_SECONDS = 21.0

# 服务端掐断连接（aiohttp ServerDisconnectedError）是瞬时故障，不该按限频
# 退避 21 秒，更不该让整段记忆抽取直接失败。
_TRANSIENT_ERRORS = (
    voyage_error.APIConnectionError,
    voyage_error.ServerError,
    voyage_error.ServiceUnavailableError,
    asyncio.TimeoutError,
    aiohttp.ClientError,
)
_TRANSIENT_RETRIES = 2
_TRANSIENT_BACKOFF_SECONDS = 2.0


async def _embed_with_retry(call):
    rate_delay = _RATE_LIMIT_BACKOFF_SECONDS
    transient_delay = _TRANSIENT_BACKOFF_SECONDS
    rate_attempts = 0
    transient_attempts = 0

    while True:
        try:
            return await call()
        except voyage_error.RateLimitError:
            rate_attempts += 1
            if rate_attempts > _RATE_LIMIT_RETRIES:
                raise
            logger.warning("VoyageAI rate limited; retrying in %.0fs", rate_delay)
            await asyncio.sleep(rate_delay)
            rate_delay = min(rate_delay * 2, 60.0)
        except _TRANSIENT_ERRORS as exc:
            transient_attempts += 1
            if transient_attempts > _TRANSIENT_RETRIES:
                raise
            logger.warning(
                "VoyageAI connection dropped (%s); retrying in %.1fs",
                exc,
                transient_delay,
            )
            await asyncio.sleep(transient_delay)
            transient_delay *= 2


def init_embedding_client(settings: Settings) -> None:
    """Initialise the async VoyageAI client (call from app lifespan)."""
    global _client  # noqa: PLW0603
    if not settings.voyageai_api_key:
        logger.info("VOYAGEAI_API_KEY not set — embedding service disabled")
        return
    _client = voyageai.AsyncClient(api_key=settings.voyageai_api_key)
    logger.info("VoyageAI embedding client initialised (model=%s)", settings.voyageai_model)


async def generate_embedding(text: str, model: str) -> list[float]:
    """Embed a single text string and return the vector."""
    if _client is None:
        raise RuntimeError("VoyageAI 客户端尚未初始化；请设置 VOYAGEAI_API_KEY")

    result = await _embed_with_retry(lambda: _client.embed([text], model=model))
    return result.embeddings[0]


async def generate_embeddings(texts: list[str], model: str) -> list[list[float]]:
    """Embed a batch of text strings and return their vectors."""
    if _client is None:
        raise RuntimeError("VoyageAI 客户端尚未初始化；请设置 VOYAGEAI_API_KEY")

    if not texts:
        return []

    result = await _embed_with_retry(lambda: _client.embed(texts, model=model))
    return result.embeddings
