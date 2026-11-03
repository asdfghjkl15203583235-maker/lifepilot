"""Shared state that flows through every node in the agent graph."""

import operator
import uuid
from typing import Annotated, Any

from langchain_core.messages import BaseMessage
from typing_extensions import TypedDict

# Merge key for HITL draft summaries collected from parallel agent nodes.
# Each node contributes its own list; operator.add concatenates them.
_pending_merge = operator.add


def merge_step_results(
    left: dict[str, str] | None,
    right: dict[str, str] | None,
) -> dict[str, str]:
    """Merge per-step outputs written by concurrently running agents."""
    return {**(left or {}), **(right or {})}


class AgentState(TypedDict, total=False):
    """All nodes read from and write to this single dict."""

    # ── Session ───────────────────────────────────────────────────────────
    user_id: uuid.UUID
    # Client conversation id of this turn. PendingAction rows tag themselves
    # with it so an unconfirmed draft can be restored after a reload.
    conversation_id: str | None
    messages: list[BaseMessage]
    is_voice_request: bool
    memory_context: str

    # ── Core intent (business state, survives follow-up turns) ────────────
    # Derived from the full history before the graph runs: what this
    # conversation is about (topic/route/entities) and whether the latest
    # utterance is a follow-up that must inherit it. Injected into every
    # agent prompt so the model reads facts, not archaeology.
    core_intent: dict

    # ── Orchestration ─────────────────────────────────────────────────────
    tasks: list[dict]   # [{intent: str, route: str}]
    current_task: dict  # per-Send sub-state, set by fan-out
    upstream_context: str  # rendered outputs of this task's depends_on steps
    plan: dict          # planner plan metadata: plan_id, summary, iteration
    supervisor_decision: dict
    completed_task_ids: list[str]  # keys of finished/ready tasks

    # ── Cross-step data flow ──────────────────────────────────────────────
    # task_key -> final output text of that step. Downstream steps receive
    # their `depends_on` predecessors' outputs as "upstream results" context,
    # so a later email step can write about the weather an earlier step fetched.
    step_results: Annotated[dict[str, str], merge_step_results]

    # ── Replanning ────────────────────────────────────────────────────────
    # Supervisor sets this when a step failed; the planner must ignore the
    # existing plan/tasks and re-plan from the failure context. The planner
    # clears it again so the next turn plans normally.
    force_replan: bool
    replan_context: dict  # {failed: [...], pending: [...], completed: [...]}

    # ── Parallel agent results — operator.add merges from concurrent agents
    agent_results: Annotated[list[dict], operator.add]

    # ── Schedule conflicts detected while persisting planner tasks ───────
    # Pre-rendered user-facing warnings; merged into the final answer so a
    # double-booking is never silently accepted.
    schedule_conflicts: Annotated[list[str], operator.add]

    # ── Steps skipped because an upstream step failed ────────────────────
    # When a DAG step fails (e.g. "火星" has no weather), its dependents must
    # not run on missing data — they get dropped and this notice tells the user.
    blocked_step_notices: Annotated[list[str], operator.add]

    # ── HITL confirmation drafts — summaries emitted as SSE pending_action events
    pending_actions: Annotated[list[dict], _pending_merge]

    # ── Final output ──────────────────────────────────────────────────────
    final_answer: str
    error: str | None


class AgentRuntimeContext(TypedDict, total=False):
    """Run-scoped values LangGraph must not persist in checkpoints.

    The graph checkpointer msgpack-serializes state, so database sessions and
    OAuth/settings objects cannot remain in AgentState. LangGraph injects
    these objects into `Runtime.context` for the current run only.
    """

    db_session: Any
    settings: Any
    gmail_access_token: str | None
    calendar_access_token: str | None
    notion_access_token: str | None
    email_credentials: Any
