import unittest

from langchain_core.language_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage

from agents.stream_collector import astream_invoke


class StreamInvokeTests(unittest.IsolatedAsyncioTestCase):
    async def test_astream_invoke_returns_assembled_message(self):
        llm = GenericFakeChatModel(messages=iter([AIMessage(content="streamed reply")]))
        response = await astream_invoke(llm, [HumanMessage(content="hi")])
        self.assertEqual(response.content, "streamed reply")

    async def test_astream_invoke_single_chunk(self):
        llm = GenericFakeChatModel(messages=iter([AIMessage(content="plain")]))
        response = await astream_invoke(llm, [HumanMessage(content="hi")])
        self.assertEqual(response.content, "plain")
