"""断线兜底：客户端切页中断 SSE 时，后端仍要把这一轮完整跑完并写入数据库。

线上故障：用户发消息后切页，前端丢弃流式连接，Starlette 在 yield 处关闭
异步生成器，`stream_chat_agent` 循环之后的落库代码再也不执行，于是"消息丢了"。

修复方式不是"断了再补写一次"，而是把生成本身搬出请求生命周期：图执行 + 落库
跑在独立 task、独立数据库会话里，SSE 只是订阅者。订阅者被取消不影响生产端。
"""

import asyncio
import unittest
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from config import Settings


def _settings() -> Settings:
    return Settings(openrouter_api_key="test", database_url="postgresql+asyncpg://x/y")


class _SlowGraph:
    """产出一点 token 后继续干活——模拟"客户端走了但后端还在生成"。"""

    def __init__(self) -> None:
        self.finished = asyncio.Event()
        self.started = False

    async def astream_events(self, *_args, **_kwargs):
        self.started = True
        yield {
            "event": "on_chain_start",
            "metadata": {"langgraph_node": "orchestrate"},
            "data": {},
        }
        await asyncio.sleep(0.05)
        yield {
            "event": "on_chain_end",
            "metadata": {"langgraph_node": "merge_results"},
            "data": {"output": {"final_answer": "明天晴，18-26℃。"}},
        }
        self.finished.set()


def _patches(graph, persist_mock):
    from services import agent_runner

    return (
        patch.object(agent_runner, "persist_chat_transcript", new=persist_mock),
        patch.object(
            agent_runner, "get_compiled_agent_graph", new=AsyncMock(return_value=graph)
        ),
        patch.object(agent_runner, "build_agent_graph_run_config", return_value={}),
        patch.object(agent_runner, "langfuse_trace_scope"),
        patch.object(
            agent_runner, "get_all_tokens", new=AsyncMock(return_value=(None, None, None))
        ),
    )


class TranscriptRescueTests(unittest.IsolatedAsyncioTestCase):
    async def test_client_disconnect_does_not_stop_generation_or_persist(self) -> None:
        from services import agent_runner

        graph = _SlowGraph()
        persist_mock = AsyncMock()
        p1, p2, p3, p4, p5 = _patches(graph, persist_mock)

        with p1, p2, p3, p4, p5:
            stream = agent_runner.stream_chat_agent(
                settings=_settings(),
                session=AsyncMock(),
                user_id=uuid.uuid4(),
                message_payloads=[{"role": "user", "content": "帮我查一下明天天气"}],
                client_conversation_id="conv-123",
            )
            # 收到第一个事件后"切页"：模拟 Starlette 关闭生成器。
            first = await stream.__anext__()
            self.assertIn("status", first)
            await stream.aclose()

            # 生产端不受影响，必须继续跑完。
            await asyncio.wait_for(graph.finished.wait(), timeout=2)

        # 完整对话必须落库，且带真实回答。
        full = [
            call
            for call in persist_mock.await_args_list
            if call.kwargs.get("final_answer")
        ]
        self.assertEqual(len(full), 1)
        self.assertEqual(full[0].kwargs["final_answer"], "明天晴，18-26℃。")
        self.assertEqual(full[0].kwargs["client_conversation_id"], "conv-123")

    async def test_successful_stream_persists_full_transcript(self) -> None:
        from services import agent_runner

        graph = _SlowGraph()
        persist_mock = AsyncMock()
        p1, p2, p3, p4, p5 = _patches(graph, persist_mock)

        with p1, p2, p3, p4, p5:
            events = [
                event
                async for event in agent_runner.stream_chat_agent(
                    settings=_settings(),
                    session=AsyncMock(),
                    user_id=uuid.uuid4(),
                    message_payloads=[{"role": "user", "content": "明天天气"}],
                    client_conversation_id="conv-456",
                )
            ]

        self.assertIn('"type": "done"', events[-1])
        # 首尾各落库一次：入口先存用户问题，收尾再存完整对话。
        self.assertEqual(persist_mock.await_count, 2)
        self.assertEqual(
            persist_mock.await_args.kwargs["final_answer"], "明天晴，18-26℃。"
        )

    async def test_uses_its_own_session_not_the_request_session(self) -> None:
        """请求级会话在断线时会被回滚，整轮副作用必须落在独立会话里。"""
        from services import agent_runner

        graph = _SlowGraph()
        persist_mock = AsyncMock()
        request_session = AsyncMock()

        own_session = AsyncMock()
        session_maker = MagicMock()
        session_maker.return_value.__aenter__ = AsyncMock(return_value=own_session)
        session_maker.return_value.__aexit__ = AsyncMock(return_value=False)

        p1, p2, p3, p4, p5 = _patches(graph, persist_mock)
        with p1, p2, p3, p4, p5, patch.object(
            agent_runner, "_get_session_maker_for_turn", return_value=session_maker, create=True
        ):
            async for _ in agent_runner.stream_chat_agent(
                settings=_settings(),
                session=request_session,
                user_id=uuid.uuid4(),
                message_payloads=[{"role": "user", "content": "明天天气"}],
                client_conversation_id="conv-789",
            ):
                pass

        for call in persist_mock.await_args_list:
            self.assertIs(call.kwargs["session"], own_session)
        own_session.commit.assert_awaited()


if __name__ == "__main__":
    unittest.main()
