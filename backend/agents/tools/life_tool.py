"""Life-service tool wrapper for the direct agent.

This keeps weather/map access provider-agnostic and lets the direct agent answer
life-service questions without learning about REST clients directly.
"""

import json
import logging
from typing import Any, Literal

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from config import Settings
from sqlalchemy.ext.asyncio import AsyncSession

from services.life_service_client import (
    get_map_context,
    get_route_planning,
    get_weather,
)

logger = logging.getLogger(__name__)


class GetWeatherArgs(BaseModel):
    city: str | None = Field(
        default=None,
        description=(
            "City name, e.g. 上海/北京. Amap resolves it to an adcode automatically."
        ),
    )
    latitude: float | None = None
    longitude: float | None = None


class GetMapContextArgs(BaseModel):
    query: str = Field(description="Place or address to look up on the map (景点/场馆/地址).")
    city: str | None = Field(
        default=None,
        description=(
            "City to scope the search to, e.g. 上海. Always pass the city the user is "
            "asking about — without it Amap matches same-name places nationwide "
            "(外滩 → 武汉/福州)."
        ),
    )


class RoutePlanningArgs(BaseModel):
    origin_latitude: float = Field(description="起点纬度, e.g. 31.2397")
    origin_longitude: float = Field(description="起点经度, e.g. 121.4903")
    destination_latitude: float = Field(description="终点纬度, e.g. 31.2323")
    destination_longitude: float = Field(description="终点经度, e.g. 121.4750")
    mode: Literal["walking", "transit", "driving"] = Field(
        default="walking",
        description="出行方式：walking 步行 / transit 公交地铁 / driving 驾车。",
    )
    city: str | None = Field(
        default=None,
        description="城市名（公交/地铁必填），如 上海。",
    )


class CreateTaskArgs(BaseModel):
    title: str = Field(description="任务标题，简短明确，例如「上海两日游」。")
    due_at: str | None = Field(
        default=None,
        description="截止/开始时间，ISO-8601（如 2026-11-07T09:00:00+08:00）；可省略。",
    )
    description: str | None = Field(
        default=None,
        description="任务详情，可放行程安排、景点顺序、注意事项。",
    )


class ItineraryItem(BaseModel):
    title: str = Field(description="单条行程节点，如「天安门广场」。")
    due_at: str | None = Field(
        default=None,
        description="ISO-8601 时间（如 2026-11-07T09:00:00+08:00）。",
    )
    description: str | None = Field(default=None, description="备注/玩法。")


class CreateTasksArgs(BaseModel):
    tasks: list[ItineraryItem] = Field(
        min_length=1,
        max_length=20,
        description="把整份行程拆成多个节点，一次性落库。",
    )


class LifeTool:
    """Collection of callable life-service tools bound to a session/settings pair."""

    def __init__(
        self,
        session: AsyncSession | None,
        settings: Settings,
        *,
        default_city: str | None = None,
        user_id: Any = None,
    ) -> None:
        """`default_city` 是会话 core_intent 里已确认的目的地（如"成都"）。

        跨轮追问「酒店定在太古里」时，本轮 intent 里没有城市，高德会命中其他
        城市的同名地点。调用方把已确认目的地传进来，查询前作为城市范围下发。

        `user_id` 只用于 `create_task` 工具（把行程/安排落库）；没有数据库会话
        或用户时该工具不会挂载。
        """
        self._session = session
        self._settings = settings
        self._default_city = (default_city or "").strip() or None
        self._user_id = user_id
        # 本轮内第一次确定的城市。真实 badcase：用户问"今天的天气 + 找个去处"，
        # 天气解析出北京、地点查询却回落成配置默认城市上海，同轮两个城市互相打架。
        # LifeTool 实例按请求创建（life_agent_node / direct_reply 各自 new 一个），
        # 用它做"本轮会话城市"是安全的作用域。
        self._turn_city: str | None = None

    def tools(self) -> list[StructuredTool]:
        tools = [
            StructuredTool.from_function(
                name="get_weather",
                description=(
                    "Get current weather for a city or coordinates. Uses Amap (高德) "
                    "in mainland China, so Chinese city names such as 上海/北京/杭州 "
                    "work directly — pass them as `city`."
                ),
                coroutine=self._weather,
                args_schema=GetWeatherArgs,
            ),
            StructuredTool.from_function(
                name="get_map_context",
                description=(
                    "按关键词查地点并返回坐标/所在城市（POI 搜索，景点、博物馆、商场、"
                    "车站都能查到）。使用高德地图。务必传 `city` 限定城市，否则会命中"
                    "全国同名地点。"
                ),
                coroutine=self._map,
                args_schema=GetMapContextArgs,
            ),
            StructuredTool.from_function(
                name="get_route_planning",
                description=(
                    "规划两地之间的路线，返回距离与预计耗时（步行/公交地铁/驾车）。"
                    "输入起点和终点的经纬度（先用 get_map_context 查到坐标）。"
                    "给行程排顺序、估算景点间通行时间时必须调用它。"
                ),
                coroutine=self._route,
                args_schema=RoutePlanningArgs,
            ),
        ]
        tools.extend(self._create_task_tools())
        return tools

    def _create_task_tools(self) -> list[StructuredTool]:
        """行程落库工具：单条 + 批量。没有 db session / user_id 时不挂载。"""
        if self._session is None or self._user_id is None:
            return []
        return [
            StructuredTool.from_function(
                name="create_task",
                description=(
                    "把一条安排落库成看板任务（低风险，直接生效）。只有在用户明确要求"
                    "「记下来 / 加到待办 / 建个任务」，或明确要把行程存成任务时调用；"
                    "只是询问行程规划时不要建任务。"
                ),
                coroutine=self._create_task,
                args_schema=CreateTaskArgs,
            ),
            StructuredTool.from_function(
                name="create_tasks",
                description=(
                    "把整份行程一次性落库成多条任务（每个景点/时段一条，带 ISO-8601 "
                    "due_at）。用户说「把行程记到待办」时优先用它，而不是重复调 "
                    "create_task。"
                ),
                coroutine=self._create_tasks,
                args_schema=CreateTasksArgs,
            ),
        ]

    def _pin_turn_city(self, city: str | None) -> None:
        """本轮内第一次确定的城市钉住，供后续同轮工具调用复用（先到先得）。"""
        text = (city or "").strip()
        if text and not self._turn_city:
            self._turn_city = text

    async def _weather(self, city: str | None = None, latitude: float | None = None, longitude: float | None = None) -> str:
        if city:
            self._pin_turn_city(city)
            return str(await get_weather(self._settings, city=city, latitude=latitude, longitude=longitude))
        if latitude is None and longitude is None:
            # 城市优先级：本轮点名 > 会话目的地 > 本轮已确定城市 > 服务端按坐标/默认城市解析。
            scoped = self._default_city or self._turn_city
            if scoped:
                self._pin_turn_city(scoped)
                return str(await get_weather(self._settings, city=scoped))
        payload = await get_weather(self._settings, latitude=latitude, longitude=longitude)
        # 坐标/默认城市解析出的实际城市也要钉住：同轮的地点查询必须跟天气同城。
        if isinstance(payload, dict):
            self._pin_turn_city(payload.get("city"))
        return str(payload)

    def _scope_city(self, city: str | None) -> str | None:
        """本轮点名的城市 > 会话已确认目的地 > 本轮已确定的城市（如天气解析结果）。"""
        explicit = (city or "").strip()
        if explicit:
            self._pin_turn_city(explicit)
            return explicit
        return self._default_city or self._turn_city

    async def _map(self, query: str, city: str | None = None) -> str:
        return str(
            await get_map_context(
                self._settings,
                query=(query or "").strip(),
                city=self._scope_city(city),
            )
        )

    async def _route(
        self,
        origin_latitude: float,
        origin_longitude: float,
        destination_latitude: float,
        destination_longitude: float,
        mode: str = "walking",
        city: str | None = None,
    ) -> str:
        return str(
            await get_route_planning(
                self._settings,
                origin_latitude=origin_latitude,
                origin_longitude=origin_longitude,
                destination_latitude=destination_latitude,
                destination_longitude=destination_longitude,
                mode=mode,
                city=self._scope_city(city),
            )
        )

    async def _run_task_tool(self, tool_name: str, args: dict[str, Any]) -> str:
        from agents.tools.task_action_tools import make_task_action_tools

        if self._session is None or self._user_id is None:
            return json.dumps(
                {"status": "error", "message": "当前会话没有数据库上下文，无法创建任务。"},
                ensure_ascii=False,
            )
        # 复用 direct 路由里的创建逻辑，保持"标题/截止时间解析 + 幂等"只有一份实现。
        for tool in make_task_action_tools(self._session, self._user_id, self._settings):
            if tool.name == tool_name:
                return await tool.ainvoke(args)
        return json.dumps({"status": "error", "message": "任务工具不可用。"}, ensure_ascii=False)

    async def _create_task(
        self,
        title: str,
        due_at: str | None = None,
        description: str | None = None,
    ) -> str:
        return await self._run_task_tool(
            "create_task",
            {"title": title, "due_at": due_at, "description": description},
        )

    async def _create_tasks(self, tasks: list[Any]) -> str:
        normalized = [
            item.model_dump() if isinstance(item, BaseModel) else item
            for item in (tasks or [])
        ]
        return await self._run_task_tool("create_tasks", {"tasks": normalized})
