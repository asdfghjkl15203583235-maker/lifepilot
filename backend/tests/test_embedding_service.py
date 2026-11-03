import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp
from voyageai.error import RateLimitError

from services import embedding_service


class _FakeClient:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    async def embed(self, texts, model):
        self.calls += 1
        if self.calls <= self.failures:
            raise RateLimitError("rate limited")
        return type("Result", (), {"embeddings": [[0.1, 0.2] for _ in texts]})()


class EmbeddingRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_retries_after_rate_limit(self) -> None:
        client = _FakeClient(failures=1)
        with patch.object(embedding_service, "_client", client), patch.object(
            embedding_service.asyncio, "sleep", AsyncMock()
        ) as sleep:
            vector = await embedding_service.generate_embedding("拿铁", model="voyage-3")

        self.assertEqual(vector, [0.1, 0.2])
        self.assertEqual(client.calls, 2)
        sleep.assert_awaited_once()

    async def test_retries_after_connection_drop(self) -> None:
        """ServerDisconnectedError 是瞬时故障，不该直接让记忆抽取失败。"""

        class FlakyNetworkClient:
            def __init__(self) -> None:
                self.calls = 0

            async def embed(self, texts, model):
                self.calls += 1
                if self.calls == 1:
                    raise aiohttp.ServerDisconnectedError("server disconnected")
                return SimpleNamespace(embeddings=[[0.3, 0.4] for _ in texts])

        client = FlakyNetworkClient()
        with patch.object(embedding_service, "_client", client), patch.object(
            embedding_service.asyncio, "sleep", AsyncMock()
        ) as sleep:
            vector = await embedding_service.generate_embedding("拿铁", model="voyage-3")

        self.assertEqual(vector, [0.3, 0.4])
        self.assertEqual(client.calls, 2)
        sleep.assert_awaited_once()

    async def test_raises_after_retries_exhausted(self) -> None:
        client = _FakeClient(failures=99)
        with patch.object(embedding_service, "_client", client), patch.object(
            embedding_service.asyncio, "sleep", AsyncMock()
        ):
            with self.assertRaises(RateLimitError):
                await embedding_service.generate_embedding("拿铁", model="voyage-3")

        self.assertEqual(client.calls, embedding_service._RATE_LIMIT_RETRIES + 1)


if __name__ == "__main__":
    unittest.main()
