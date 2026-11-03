"""LangGraph 代理图 — LifePilot 规划器/监督代理并行编排器。

图形结构：

    orchestrate → planner
                    │
                    └── [Send 并行分发 — 每次只发送一个就绪的依赖层]
                         ├── email_agent
                         ├── calendar_agent
                         ├── notion_agent
                         ├── life_agent
                         └── direct_reply
                                    │
                               supervisor
                                    │  ├─ 还有就绪任务 → 再次并行分发
                                    │  ├─ 任务受阻 → 重新规划循环（planner）
                                    │  └─ 完成 → merge_results → END
"""

from functools import partial
from typing import Any

from langchain_openai import ChatOpenAI
from langgraph.constants import Send
from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph

from agents.calendar_agent_node import calendar_agent_node
from agents.email_agent_node import email_agent_node
from agents.life_agent_node import life_agent_node
from agents.notion_agent_node import notion_agent_node
from agents.orchestrator_node import (
    ROUTE_CALENDAR,
    ROUTE_DIRECT,
    ROUTE_EMAIL,
    ROUTE_LIFE,
    ROUTE_NOTION,
    ROUTE_SEARCH,
    direct_reply_node,
    merge_results_node,
    orchestrate_node,
)
from agents.search_agent_node import search_agent_node
from agents.common import upstream_results_context
from agents.planning import is_ready
from agents.planner_agent_node import planner_node
from agents.state import AgentRuntimeContext, AgentState
from agents.supervisor_node import supervisor_node

NODE_ORCHESTRATE = "orchestrate"
NODE_PLANNER = "planner"
NODE_EMAIL = "email_agent"
NODE_CALENDAR = "calendar_agent"
NODE_NOTION = "notion_agent"
NODE_DIRECT = "direct_reply"
NODE_LIFE = "life_agent"
NODE_SEARCH = "search_agent"
NODE_SUPERVISOR = "supervisor"
NODE_MERGE = "merge_results"

_ROUTE_TO_NODE = {
    ROUTE_EMAIL: NODE_EMAIL,
    ROUTE_CALENDAR: NODE_CALENDAR,
    ROUTE_NOTION: NODE_NOTION,
    ROUTE_DIRECT: NODE_DIRECT,
    ROUTE_LIFE: NODE_LIFE,
    ROUTE_SEARCH: NODE_SEARCH,
}


def _fan_out(state: AgentState) -> list[Send]:
    """只分发下一个就绪的依赖层；后续层在其完成后运行。"""
    tasks = list(state.get("tasks") or [])
    if not tasks:
        return [Send(NODE_DIRECT, {**state, "current_task": {"intent": "", "route": ROUTE_DIRECT}})]

    completed_ids = set(state.get("completed_task_ids") or [])
    ready = [
        task
        for task in tasks
        if is_ready(task, completed_ids)
        and str(task.get("task_key") or task.get("id") or "") not in completed_ids
    ]
    if not ready:
        return [Send(NODE_DIRECT, {**state, "current_task": {"intent": "", "route": ROUTE_DIRECT}})]

    step_results = state.get("step_results") or {}
    tasks = list(state.get("tasks") or [])
    return [
        Send(
            _ROUTE_TO_NODE.get(task.get("route", ROUTE_DIRECT), NODE_DIRECT),
            {
                **state,
                "current_task": task,
                # 下游步骤带着上游产出跑：没有依赖的步骤得到空串。
                "upstream_context": upstream_results_context(task, step_results, tasks),
            },
        )
        for task in ready
    ]


def build_graph(
    llm: ChatOpenAI,
    *,
    checkpointer: Any | None = None,
) -> CompiledStateGraph:
    """构建并编译带规划器和监督代理的并行编排图。

    布局：

        orchestrate → planner → [按依赖层并行分发]
                                 ├── email / calendar / notion / life / direct
                                 │
                                supervisor
                                 │  ├─ 受阻？→ 规划器/监督代理重新规划循环
                                 │  └─ 完成 → merge_results → END
    """
    graph = StateGraph(AgentState, context_schema=AgentRuntimeContext)

    graph.add_node(NODE_ORCHESTRATE, partial(orchestrate_node, llm=llm))
    graph.add_node(NODE_PLANNER, partial(planner_node, llm=llm))
    graph.add_node(NODE_EMAIL, partial(email_agent_node, llm=llm))
    graph.add_node(NODE_CALENDAR, partial(calendar_agent_node, llm=llm))
    graph.add_node(NODE_NOTION, partial(notion_agent_node, llm=llm))
    graph.add_node(NODE_DIRECT, partial(direct_reply_node, llm=llm))
    graph.add_node(NODE_LIFE, partial(life_agent_node, llm=llm))
    graph.add_node(NODE_SEARCH, partial(search_agent_node, llm=llm))
    graph.add_node(NODE_SUPERVISOR, partial(supervisor_node, llm=llm))
    graph.add_node(NODE_MERGE, partial(merge_results_node, llm=llm))

    graph.set_entry_point(NODE_ORCHESTRATE)
    graph.add_edge(NODE_ORCHESTRATE, NODE_PLANNER)
    graph.add_conditional_edges(
        NODE_PLANNER,
        _fan_out,
        [NODE_EMAIL, NODE_CALENDAR, NODE_NOTION, NODE_DIRECT, NODE_LIFE, NODE_SEARCH],
    )
    for node in [NODE_EMAIL, NODE_CALENDAR, NODE_NOTION, NODE_DIRECT, NODE_LIFE, NODE_SEARCH]:
        graph.add_edge(node, NODE_SUPERVISOR)
    graph.add_conditional_edges(
        NODE_SUPERVISOR,
        _route_after_supervisor,
        {
            "continue": NODE_PLANNER,
            "replan": NODE_PLANNER,
            "done": NODE_MERGE,
        },
    )
    graph.add_edge(NODE_MERGE, END)

    return graph.compile(checkpointer=checkpointer)


def _route_after_supervisor(state: AgentState) -> str:
    """当监督代理发现没有需要重跑的内容时，继续进入合并节点。"""
    action = (state.get("supervisor_decision") or {}).get("action")
    if action in {"continue", "replan"}:
        return action
    return "done"
