"""DeepSeek 结构化输出兼容层。

真实报错：`This response_format type is unavailable now`。
根因是 LangChain `with_structured_output()` 默认 method="json_schema"，给 DeepSeek
发了 `{"type": "json_schema", ...}`，而它只接受 `{"type": "json_object"}`。

这里锁住三层行为：
  1. provider 判断（base_url 含 deepseek）；
  2. 请求 payload 兜底降级 json_schema → json_object；
  3. DeepSeek 走 prompt 引导 + 手动 JSON 解析，且 prompt 里必须出现 "json"。
"""

import unittest
from unittest.mock import AsyncMock, MagicMock

from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel

from config import Settings, chat_llm
from services.structured_output import (
    JSON_OBJECT_RESPONSE_FORMAT,
    build_json_messages,
    downgrade_response_format,
    extract_json_payload,
    invoke_structured_json,
    is_deepseek_base_url,
    is_deepseek_model,
)

DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


class _Sample(BaseModel):
    title: str | None = None
    starts_at: str | None = None


def _fake_llm(base_url: str) -> MagicMock:
    llm = MagicMock()
    llm.openai_api_base = base_url
    return llm


class ProviderDetectionTests(unittest.TestCase):
    def test_deepseek_detected_from_base_url(self) -> None:
        self.assertTrue(is_deepseek_base_url(DEEPSEEK_BASE_URL))
        self.assertTrue(is_deepseek_base_url("https://api.deepseek.com"))
        self.assertFalse(is_deepseek_base_url(OPENROUTER_BASE_URL))
        self.assertFalse(is_deepseek_base_url(None))

    def test_model_detection_reads_openai_api_base(self) -> None:
        self.assertTrue(is_deepseek_model(_fake_llm(DEEPSEEK_BASE_URL)))
        self.assertFalse(is_deepseek_model(_fake_llm(OPENROUTER_BASE_URL)))

    def test_chat_llm_disables_json_schema_for_deepseek(self) -> None:
        deepseek = chat_llm(
            Settings(
                openrouter_api_key="test",
                openrouter_base_url=DEEPSEEK_BASE_URL,
                openrouter_default_model="deepseek-chat",
            )
        )
        self.assertFalse(deepseek.supports_json_schema)

        openrouter = chat_llm(
            Settings(
                openrouter_api_key="test",
                openrouter_base_url=OPENROUTER_BASE_URL,
                openrouter_default_model="anthropic/claude-sonnet-4.6",
            )
        )
        self.assertTrue(openrouter.supports_json_schema)


class PayloadDowngradeTests(unittest.TestCase):
    def test_json_schema_dict_is_downgraded(self) -> None:
        payload = {
            "model": "deepseek-chat",
            "response_format": {"type": "json_schema", "json_schema": {"name": "x"}},
        }
        downgraded = downgrade_response_format(payload)
        self.assertEqual(downgraded["response_format"], JSON_OBJECT_RESPONSE_FORMAT)

    def test_pydantic_class_is_downgraded(self) -> None:
        # with_structured_output() 绑定的是类本身，OpenAI SDK 发请求前才转成 json_schema。
        payload = {"response_format": _Sample}
        downgraded = downgrade_response_format(payload)
        self.assertEqual(downgraded["response_format"], JSON_OBJECT_RESPONSE_FORMAT)

    def test_json_object_and_text_are_untouched(self) -> None:
        for original in ({"type": "json_object"}, {"type": "text"}):
            payload = {"response_format": dict(original)}
            self.assertEqual(downgrade_response_format(payload)["response_format"], original)

    def test_missing_response_format_is_safe(self) -> None:
        self.assertEqual(downgrade_response_format({"model": "x"}), {"model": "x"})

    def test_real_request_payload_never_sends_json_schema_to_deepseek(self) -> None:
        """端到端回归：with_structured_output 绑定的类不会以 json_schema 形式发给 DeepSeek。"""
        deepseek = chat_llm(
            Settings(
                openrouter_api_key="test",
                openrouter_base_url=DEEPSEEK_BASE_URL,
                openrouter_default_model="deepseek-chat",
            )
        )
        payload = deepseek._get_request_payload(
            [HumanMessage(content="hi")], response_format=_Sample
        )
        self.assertEqual(payload["response_format"], JSON_OBJECT_RESPONSE_FORMAT)

        openrouter = chat_llm(
            Settings(
                openrouter_api_key="test",
                openrouter_base_url=OPENROUTER_BASE_URL,
                openrouter_default_model="gpt-4o",
            )
        )
        payload = openrouter._get_request_payload(
            [HumanMessage(content="hi")], response_format=_Sample
        )
        # 非 DeepSeek 保持原生结构化输出，不做降级。
        self.assertIs(payload["response_format"], _Sample)


class JsonParsingTests(unittest.TestCase):
    def test_plain_json(self) -> None:
        self.assertEqual(extract_json_payload('{"a": 1}'), {"a": 1})

    def test_fenced_json(self) -> None:
        self.assertEqual(extract_json_payload('```json\n{"a": 2}\n```'), {"a": 2})

    def test_json_with_surrounding_prose(self) -> None:
        self.assertEqual(extract_json_payload('结果如下：{"a": 3} 以上。'), {"a": 3})

    def test_non_json_returns_none(self) -> None:
        self.assertIsNone(extract_json_payload("抱歉，我不知道。"))
        self.assertIsNone(extract_json_payload(""))


class JsonPromptContractTests(unittest.TestCase):
    def test_prompt_mentions_json_and_carries_schema(self) -> None:
        messages = build_json_messages("你是抽取器。", "邮件正文", _Sample)
        system_text = messages[0].content
        self.assertIn("json", system_text.lower())
        self.assertIn("title", system_text)
        self.assertEqual(messages[1].content, "邮件正文")


class InvokeStructuredJsonTests(unittest.IsolatedAsyncioTestCase):
    async def test_deepseek_uses_json_object_mode_and_parses_reply(self) -> None:
        llm = _fake_llm(DEEPSEEK_BASE_URL)
        llm.bind.return_value.ainvoke = AsyncMock(
            return_value=AIMessage(content='```json\n{"title": "评审会", "starts_at": null}\n```')
        )

        result = await invoke_structured_json(
            llm, _Sample, system_prompt="抽取事件。", human_prompt="下周二评审"
        )

        self.assertIsInstance(result, _Sample)
        self.assertEqual(result.title, "评审会")
        llm.with_structured_output.assert_not_called()
        llm.bind.assert_called_once_with(response_format=JSON_OBJECT_RESPONSE_FORMAT)
        prompt = llm.bind.return_value.ainvoke.await_args.args[0][0].content
        self.assertIn("json", prompt.lower())

    async def test_deepseek_invalid_json_returns_none(self) -> None:
        llm = _fake_llm(DEEPSEEK_BASE_URL)
        llm.bind.return_value.ainvoke = AsyncMock(return_value=AIMessage(content="没有 JSON"))

        self.assertIsNone(
            await invoke_structured_json(
                llm, _Sample, system_prompt="抽取事件。", human_prompt="正文"
            )
        )

    async def test_non_deepseek_prefers_structured_output(self) -> None:
        llm = _fake_llm(OPENROUTER_BASE_URL)
        llm.with_structured_output.return_value.ainvoke = AsyncMock(
            return_value=_Sample(title="评审会")
        )

        result = await invoke_structured_json(
            llm, _Sample, system_prompt="抽取事件。", human_prompt="正文"
        )

        self.assertEqual(result.title, "评审会")
        llm.bind.assert_not_called()

    async def test_non_deepseek_falls_back_when_provider_rejects_json_schema(self) -> None:
        llm = _fake_llm(OPENROUTER_BASE_URL)
        llm.with_structured_output.return_value.ainvoke = AsyncMock(
            side_effect=RuntimeError("This response_format type is unavailable now")
        )
        llm.bind.return_value.ainvoke = AsyncMock(
            return_value=AIMessage(content='{"title": "兜底解析"}')
        )

        result = await invoke_structured_json(
            llm, _Sample, system_prompt="抽取事件。", human_prompt="正文"
        )

        self.assertEqual(result.title, "兜底解析")
        llm.bind.assert_called_once_with(response_format=JSON_OBJECT_RESPONSE_FORMAT)


if __name__ == "__main__":
    unittest.main()
