"""Provider-aware structured (JSON) output for every chat model call.

**Why this module exists.** LangChain's `ChatOpenAI.with_structured_output()` defaults to
`method="json_schema"`, which sends:

    {"response_format": {"type": "json_schema", "json_schema": {...}}}

DeepSeek's OpenAI-compatible endpoint only accepts `{"type": "json_object"}` and answers
HTTP 400 `This response_format type is unavailable now`. `json_object` mode also requires
the literal word "json" somewhere in the prompt, otherwise DeepSeek rejects the request.

So every structured call goes through `invoke_structured_json()`:

* DeepSeek → prompt-guided JSON (schema + "json" instruction in the prompt) with
  `response_format={"type": "json_object"}` bound explicitly, then manual parsing.
* Other providers → `with_structured_output()` first, with the same prompt-guided path as
  a fallback so a provider that rejects `json_schema` never surfaces as a hard failure.

`downgrade_response_format()` is the request-payload safety net used by
`config.ProviderAwareChatOpenAI`; it catches any structured call that bypasses this module.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, TypeVar

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel

logger = logging.getLogger(__name__)

DEEPSEEK_BASE_URL_MARKER = "deepseek"
JSON_OBJECT_RESPONSE_FORMAT: dict[str, str] = {"type": "json_object"}
# DeepSeek 的 json_object 模式要求 prompt 中出现 "json" 这个词，缺失会直接 400。
JSON_ONLY_INSTRUCTION = (
    "只输出一个 json 对象：不要 Markdown 代码块、不要解释、不要多余文字；"
    "无法确定的字段填 null。"
)

_FENCED_JSON_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)

SchemaT = TypeVar("SchemaT", bound=BaseModel)


def is_deepseek_base_url(base_url: str | None) -> bool:
    return DEEPSEEK_BASE_URL_MARKER in (base_url or "").lower()


def base_url_of(llm: BaseChatModel | Any) -> str:
    """Read the provider base URL from a chat model (tolerant to LangChain versions)."""
    for attribute in ("openai_api_base", "base_url"):
        value = getattr(llm, attribute, None)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def is_deepseek_model(llm: BaseChatModel | Any) -> bool:
    return is_deepseek_base_url(base_url_of(llm))


def downgrade_response_format(payload: dict[str, Any]) -> dict[str, Any]:
    """Replace an unsupported `json_schema` response_format with `json_object`.

    DeepSeek 只认 `{"type": "json_object"}`；其它类型保持原样，避免误改正常请求。

    两种形态都要处理：
      - dict `{"type": "json_schema", "json_schema": {...}}`（已转换过的）；
      - 直接传进来的 pydantic / dataclass 类（`with_structured_output()` 绑定的就是
        这个形态，OpenAI SDK 会在发请求前才把它转成 json_schema）。
    """
    response_format = payload.get("response_format")
    if response_format is None:
        return payload

    is_schema_class = isinstance(response_format, type)
    is_schema_dict = (
        isinstance(response_format, dict) and response_format.get("type") == "json_schema"
    )
    if is_schema_class or is_schema_dict:
        logger.info("Downgrading response_format json_schema -> json_object for this provider")
        payload["response_format"] = dict(JSON_OBJECT_RESPONSE_FORMAT)
    return payload


def extract_json_payload(text: str) -> dict[str, Any] | None:
    """Parse a JSON object out of an LLM reply (tolerates ```json fences and prose)."""
    if not text:
        return None
    cleaned = _FENCED_JSON_RE.sub("", text.strip()).strip()
    candidates = [cleaned]
    first_brace, last_brace = cleaned.find("{"), cleaned.rfind("}")
    if 0 <= first_brace < last_brace:
        candidates.append(cleaned[first_brace: last_brace + 1])
    for candidate in candidates:
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def build_json_messages(
    system_prompt: str,
    human_prompt: str,
    schema: type[BaseModel],
) -> list[BaseMessage]:
    """System + human messages carrying the schema and the required "json" keyword."""
    schema_text = json.dumps(schema.model_json_schema(), ensure_ascii=False)
    instructions = (
        f"{system_prompt}\n\n"
        f"请严格按下面的 json 结构输出（字段可为 null）：\n{schema_text}\n\n"
        f"{JSON_ONLY_INSTRUCTION}"
    )
    return [SystemMessage(content=instructions), HumanMessage(content=human_prompt)]


async def invoke_structured_json(
    llm: BaseChatModel,
    schema: type[SchemaT],
    *,
    system_prompt: str,
    human_prompt: str,
) -> SchemaT | None:
    """Ask the model for JSON matching `schema`; None when the reply is unusable."""
    messages = build_json_messages(system_prompt, human_prompt, schema)

    if not is_deepseek_model(llm):
        try:
            response = await llm.with_structured_output(schema).ainvoke(messages)
            if isinstance(response, schema):
                return response
            logger.warning(
                "Structured output returned %s instead of %s; falling back to JSON prompt",
                type(response).__name__,
                schema.__name__,
            )
        except Exception as exc:
            # 有 provider 明确不支持 json_schema：降级到 prompt + 手动解析，
            # 而不是让整条链路失败。
            logger.warning("with_structured_output failed for %s: %s", schema.__name__, exc)

    return await _invoke_with_json_object_mode(llm, schema, messages)


async def _invoke_with_json_object_mode(
    llm: BaseChatModel,
    schema: type[SchemaT],
    messages: list[BaseMessage],
) -> SchemaT | None:
    """Request `response_format=json_object` and parse the reply ourselves."""
    try:
        bound = llm.bind(response_format=dict(JSON_OBJECT_RESPONSE_FORMAT))
        response = await bound.ainvoke(messages)
    except Exception as exc:
        logger.warning("json_object structured call failed for %s: %s", schema.__name__, exc)
        return None

    text = response.content if isinstance(response.content, str) else ""
    payload = extract_json_payload(text)
    if payload is None:
        logger.warning("Model reply is not a JSON object for %s: %.200s", schema.__name__, text)
        return None
    try:
        return schema.model_validate(payload)
    except Exception as exc:
        logger.warning("Model JSON does not match %s: %s", schema.__name__, exc)
        return None
