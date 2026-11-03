"""任务二：SSE 断线续传（自增 id + 环形缓冲 + Last-Event-ID 补发）。

真实故障：用户切页时 SSE 连接被关闭，重连后从头开始或干脆丢消息。

修复：每个会话一条事件流，事件带单调递增 id，缓冲最近 100 条；客户端重连带
`Last-Event-ID`，后端只补发 id 更大的事件；流跑完仍原子化落库。
"""

import json
import unittest
import uuid
from unittest.mock import AsyncMock, patch

from config import Settings

from tests.test_transcript_rescue import _SlowGraph, _patches


def _settings() -> Settings:
    return Settings(openrouter_api_key="test", database_url="postgresql+asyncpg://x/y")


def _parse_events(chunks: list[str]) -> list[tuple[int, dict]]:
    events: list[tuple[int, dict]] = []
    for chunk in chunks:
        event_id = None
        payload = None
        for line in chunk.split("\n"):
            if line.startswith("id: "):
                event_id = int(line[4:])
            elif line.startswith("data: "):
                payload = json.loads(line[6:])
        if event_id is not None and payload is not None:
            events.append((event_id, payload))
    return events


class StreamBufferTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        from services import agent_runner

        agent_runner.reset_stream_registry()

    async def test_events_have_monotonic_ids(self) -> None:
        from services import agent_runner

        graph = _SlowGraph()
        p1, p2, p3, p4, p5 = _patches(graph, AsyncMock())
        with p1, p2, p3, p4, p5:
            chunks = [
                chunk
                async for chunk in agent_runner.stream_chat_agent(
                    settings=_settings(),
                    session=AsyncMock(),
                    user_id=uuid.uuid4(),
                    message_payloads=[{"role": "user", "content": "明天天气"}],
                    client_conversation_id="conv-id",
                )
            ]

        events = _parse_events(chunks)
        self.assertTrue(events, "应至少产出一条事件")
        ids = [event_id for event_id, _ in events]
        self.assertEqual(ids, sorted(ids), "事件 id 必须单调递增")
        self.assertEqual(len(ids), len(set(ids)), "事件 id 不能重复")
        self.assertEqual(events[-1][1]["type"], "done")

    async def test_reconnect_replays_only_missing_events(self) -> None:
        from services import agent_runner

        graph = _SlowGraph()
        p1, p2, p3, p4, p5 = _patches(graph, AsyncMock())
        with p1, p2, p3, p4, p5:
            first = [
                chunk
                async for chunk in agent_runner.stream_chat_agent(
                    settings=_settings(),
                    session=AsyncMock(),
                    user_id=uuid.uuid4(),
                    message_payloads=[{"role": "user", "content": "明天天气"}],
                    client_conversation_id="conv-replay",
                )
            ]
            all_events = _parse_events(first)
            last_seen = all_events[0][0]

            # 重连：带 Last-Event-ID，只应补 id 更大的事件。
            replay = [
                chunk
                async for chunk in agent_runner.stream_chat_agent(
                    settings=_settings(),
                    session=AsyncMock(),
                    user_id=uuid.uuid4(),
                    message_payloads=[{"role": "user", "content": "明天天气"}],
                    client_conversation_id="conv-replay",
                    last_event_id=last_seen,
                )
            ]

        replayed = _parse_events(replay)
        self.assertTrue(replayed)
        self.assertTrue(
            all(event_id > last_seen for event_id, _ in replayed),
            "补发的事件 id 必须全部大于 Last-Event-ID",
        )
        self.assertEqual(replayed[-1][1]["type"], "done")
        self.assertEqual(
            [eid for eid, _ in replayed],
            [eid for eid, _ in all_events if eid > last_seen],
            "补发内容必须与原始事件流一致，不重不漏",
        )

    async def test_reconnect_does_not_start_a_second_producer(self) -> None:
        """重连绝不能重新跑图——否则日程/提醒会被重复创建。"""
        from services import agent_runner

        graph = _SlowGraph()
        persist_mock = AsyncMock()
        p1, p2, p3, p4, p5 = _patches(graph, persist_mock)
        with p1, p2, p3, p4, p5:
            first = [
                chunk
                async for chunk in agent_runner.stream_chat_agent(
                    settings=_settings(),
                    session=AsyncMock(),
                    user_id=uuid.uuid4(),
                    message_payloads=[{"role": "user", "content": "明天天气"}],
                    client_conversation_id="conv-single",
                )
            ]
            persist_calls_after_first = persist_mock.await_count

            [
                chunk
                async for chunk in agent_runner.stream_chat_agent(
                    settings=_settings(),
                    session=AsyncMock(),
                    user_id=uuid.uuid4(),
                    message_payloads=[{"role": "user", "content": "明天天气"}],
                    client_conversation_id="conv-single",
                    last_event_id=_parse_events(first)[0][0],
                )
            ]

        self.assertEqual(
            persist_mock.await_count,
            persist_calls_after_first,
            "重连不应重新执行（落库次数不能增加）",
        )

    async def test_unknown_stream_with_last_event_id_asks_client_to_resync(self) -> None:
        """缓冲已丢（进程重启）时不能重跑，只能让客户端回服务端拉取。"""
        from services import agent_runner

        graph = _SlowGraph()
        p1, p2, p3, p4, p5 = _patches(graph, AsyncMock())
        with p1, p2, p3, p4, p5:
            chunks = [
                chunk
                async for chunk in agent_runner.stream_chat_agent(
                    settings=_settings(),
                    session=AsyncMock(),
                    user_id=uuid.uuid4(),
                    message_payloads=[{"role": "user", "content": "明天天气"}],
                    client_conversation_id="conv-gone",
                    last_event_id=42,
                )
            ]

        events = _parse_events(chunks)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][1]["type"], "error")
        self.assertTrue(events[0][1].get("resync"))
        self.assertFalse(graph.started, "缓冲缺失时绝不能重新执行图")

    async def test_buffer_keeps_only_latest_events(self) -> None:
        from services import agent_runner

        stream = agent_runner._ConversationStream()
        for index in range(agent_runner.STREAM_BUFFER_MAX + 25):
            stream.publish({"type": "token", "content": str(index)})

        self.assertEqual(len(stream.events), agent_runner.STREAM_BUFFER_MAX)
        self.assertEqual(stream.events[-1][0], agent_runner.STREAM_BUFFER_MAX + 25)


if __name__ == "__main__":
    unittest.main()
