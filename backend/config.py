from functools import lru_cache
from pathlib import Path
from typing import Any

from langchain_core.language_models import LanguageModelInput
from langchain_openai import ChatOpenAI
from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from services.structured_output import downgrade_response_format, is_deepseek_base_url

# Resolve so SILICONFLOW_* and other keys load even when uvicorn is started from the repo root
_BACKEND_DIR = Path(__file__).resolve().parent
_DOTENV_PATH = _BACKEND_DIR / ".env"

OPENROUTER_DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_DEFAULT_MODEL = "anthropic/claude-sonnet-4.6"
CHAT_LLM_TIMEOUT_SECONDS = 120
CHAT_LLM_MAX_RETRIES = 3
VOYAGEAI_DEFAULT_MODEL = "voyage-3"

SILICONFLOW_DEFAULT_BASE_URL = "https://api.siliconflow.cn/v1"
SILICONFLOW_DEFAULT_TRANSCRIPTION_MODEL = "FunAudioLLM/SenseVoiceSmall"

EDGE_TTS_DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"

LANGSMITH_DEFAULT_PROJECT_NAME = "chief-of-staff"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_DOTENV_PATH),
        # Strip BOM from editors that save UTF-8 with BOM (breaks first variable name otherwise).
        env_file_encoding="utf-8-sig",
        # Shell vars override .env; empty exports like `export SILICONFLOW_API_KEY=` must not shadow the file.
        env_ignore_empty=True,
        extra="ignore",
    )

    @field_validator("siliconflow_api_key", mode="before")
    @classmethod
    def normalize_siliconflow_api_key(cls, value: object) -> object:
        if value is None:
            return None
        if not isinstance(value, str):
            return value
        stripped = value.strip().strip("\ufeff")
        if len(stripped) >= 2 and (
            (stripped[0] == '"' and stripped[-1] == '"')
            or (stripped[0] == "'" and stripped[-1] == "'")
        ):
            stripped = stripped[1:-1].strip()
        return stripped if stripped else None

    api_host: str = "0.0.0.0"
    api_port: int = 8000

    openrouter_api_key: str | None = None
    openrouter_base_url: str = OPENROUTER_DEFAULT_BASE_URL
    openrouter_default_model: str = OPENROUTER_DEFAULT_MODEL
    # OpenRouter reserves max_tokens against the account balance; the provider
    # default (model max, e.g. 16000) can make every call fail with HTTP 402 on
    # a low balance even though the actual reply is short.
    openrouter_max_tokens: int = 2048
    # Optional cheaper/faster model for short auxiliary passes (conversation
    # titles). Falls back to the default model when unset or when the
    # configured provider does not serve the hardcoded override.
    openrouter_utility_model: str | None = None
    # DeepSeek 的思考模式要求把上一轮的 reasoning_content 原样回传，而
    # LangChain 的 tool-call 往返不会携带该字段，于是第二轮起必然 400：
    # "The `reasoning_content` in the thinking mode must be passed back"。
    # 多轮对话 + 工具调用是本项目的主路径，这里默认关闭思考模式；
    # 需要推理深度时用 chat_thinking_mode=true 显式打开。
    chat_thinking_mode: bool = False

    cors_allowed_origins: str = (
        "http://localhost:3000,http://localhost:3001,"
        "http://127.0.0.1:3000,http://127.0.0.1:3001,"
        "http://[::1]:3000,http://[::1]:3001"
    )

    database_url: str | None = None
    database_echo_sql: bool = False
    # A chat request holds its DB session for the entire SSE stream, so the pool
    # must cover concurrent chats. Point at a transaction pooler (e.g. Supabase
    # :6543) when the server caps connections; a session pooler only allows one.
    database_pool_size: int = 10
    database_max_overflow: int = 20
    database_pool_recycle_seconds: int = 3600
    init_db_schema_on_startup: bool = False

    jwt_secret: str | None = None

    google_client_id: str | None = None
    google_client_secret: str | None = None
    google_redirect_uri: str = "http://localhost:8081/gmail/callback"
    google_calendar_redirect_uri: str = "http://localhost:8081/calendar/callback"

    # 163 邮箱授权码登录（IMAP 收信 / SMTP 发信）
    mail163_email: str | None = None
    mail163_email_auth_code: str | None = None
    mail163_smtp_host: str = "smtp.163.com"
    mail163_smtp_port: int = 465
    mail163_imap_host: str = "imap.163.com"
    mail163_imap_port: int = 993
    mail163_email_timeout_seconds: int = 10

    notion_client_id: str | None = None
    notion_client_secret: str | None = None
    notion_redirect_uri: str = "http://localhost:8081/notion/callback"
    # Internal integration token (ntn_...). Used when the user has not connected
    # Notion via OAuth; the Notion agent falls back to it for search and writes.
    notion_api_key: str | None = None

    frontend_url: str = "http://localhost:3000"

    # Local .ics calendar sync — scan this directory (relative to backend/) for *.ics files.
    local_ics_dir: str = "./local_calendar"

    redis_url: str | None = None

    voyageai_api_key: str | None = None
    voyageai_model: str = VOYAGEAI_DEFAULT_MODEL

    siliconflow_api_key: str | None = None
    siliconflow_base_url: str = SILICONFLOW_DEFAULT_BASE_URL
    siliconflow_transcription_model: str = SILICONFLOW_DEFAULT_TRANSCRIPTION_MODEL

    edge_tts_voice: str = EDGE_TTS_DEFAULT_VOICE

    # Gmail list/search — kept small to avoid blowing the LLM context window.
    # Raise via env vars only if you know your emails are short and you need more breadth.
    gmail_list_max_results: int = 20
    gmail_list_max_total_messages: int = 50
    gmail_list_max_enriched: int = 10
    gmail_list_enrich_concurrency: int = 5

    # LifePilot planner/supervisor
    planner_enabled: bool = True
    planner_max_tasks: int = 8
    planner_max_iterations: int = 4
    planner_timezone: str = "UTC"

    # Proactive reminders (deadline/meeting/task progress)
    reminder_tick_enabled: bool = True
    reminder_default_lead_minutes: int = 30

    # Tencent Meeting REST API (optional; requires developer credentials)
    tencent_meeting_app_id: str | None = None
    tencent_meeting_secret_id: str | None = None
    tencent_meeting_secret_key: str | None = None
    tencent_meeting_base_url: str = "https://api.meeting.qq.com/v1"
    tencent_meeting_user_id: str | None = None
    tencent_meeting_enabled: bool = True

    # Life services (weather/map). Open-Meteo works without a key; Amap (高德)
    # is the preferred provider inside mainland China.
    weather_provider: str = "open_meteo"
    openweather_api_key: str | None = None
    amap_api_key: str | None = None
    weather_latitude: float | None = None
    weather_longitude: float | None = None
    weather_default_city: str = "Shanghai"
    map_provider: str = "open_meteo"
    google_maps_api_key: str | None = None

    # 联网搜索（旅行攻略/避坑/最新信息）。没有 key 时 search 分支会如实报错，
    # 而不是让模型凭常识编攻略。
    tavily_api_key: str | None = None
    web_search_max_results: int = 5

    # LangGraph checkpointing (optional: "memory" = in-process, "postgres" = durable Postgres state)
    langgraph_checkpointer: str | None = None

    # LangSmith — traces graph + nested LLM/tool runs when enabled and API key is set.
    langsmith_tracing_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "LANGSMITH_TRACING_V2",
            "LANGCHAIN_TRACING_V2",
            "LANGSMITH_TRACING",
        ),
    )
    langsmith_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("LANGSMITH_API_KEY", "LANGCHAIN_API_KEY"),
    )
    langsmith_project: str | None = Field(
        default=None,
        validation_alias=AliasChoices("LANGSMITH_PROJECT", "LANGCHAIN_PROJECT"),
    )
    langsmith_api_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "LANGSMITH_ENDPOINT",
            "LANGCHAIN_ENDPOINT",
        ),
    )

    # Langfuse — full-chain LLM tracing when public+secret keys are set.
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    langfuse_host: str = "https://cloud.langfuse.com"


@lru_cache
def get_settings() -> Settings:
    return Settings()


def has_mail163_credentials(settings: Settings) -> bool:
    return bool(settings.mail163_email and settings.mail163_email_auth_code)


class ProviderAwareChatOpenAI(ChatOpenAI):
    """ChatOpenAI that never sends a `response_format` the provider cannot handle.

    DeepSeek 只接受 `{"type": "json_object"}`，收到 `json_schema` 会回 400
    `This response_format type is unavailable now`。结构化调用统一走
    `services.structured_output.invoke_structured_json`；这个子类是最后的兜底，
    覆盖任何直接 `bind(response_format=...)` / `with_structured_output()` 的调用。
    """

    # False 时把请求里的 json_schema 降级成 json_object（见 _get_request_payload）。
    supports_json_schema: bool = True

    def _get_request_payload(
        self,
        input_: LanguageModelInput,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> dict:
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        if self.supports_json_schema:
            return payload
        return downgrade_response_format(payload)


def chat_llm(settings: Settings, *, model: str | None = None) -> ChatOpenAI:
    return ProviderAwareChatOpenAI(
        model=model or settings.openrouter_default_model,
        api_key=settings.openrouter_api_key,
        base_url=settings.openrouter_base_url,
        timeout=CHAT_LLM_TIMEOUT_SECONDS,
        max_retries=CHAT_LLM_MAX_RETRIES,
        max_tokens=settings.openrouter_max_tokens,
        extra_body=_thinking_body(settings),
        supports_json_schema=not is_deepseek_base_url(settings.openrouter_base_url),
    )


def _thinking_body(settings: Settings) -> dict | None:
    """DeepSeek-only reasoning toggle.

    `thinking` is a DeepSeek extension; sending it to another provider can be
    rejected as an unknown body param, so gate on the base URL too.
    """
    if settings.chat_thinking_mode:
        return None
    if "deepseek" not in (settings.openrouter_base_url or "").lower():
        return None
    return {"thinking": {"type": "disabled"}}
