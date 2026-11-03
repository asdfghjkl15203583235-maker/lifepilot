"""四个真实 Bad Case 的回归测试（记忆假动作 / DAG 数据污染 / 冷血回复 / 情绪噪音）。

1. Bug 1（致命）：用户说"领导是李四（之前张三作废）"，旧记忆"领导是张三"仍
   `is_active=True`，只在文本里加一句"作废"备注 —— 等于没改。修复：纠错词 +
   同维度 → 旧记录必须真正失效，备注必须从落库文本里剥掉。
2. Bug 2：问"火星天气"高德模糊命中"火星村"，助手仍拿别处数据写邮件。修复：
   实体不一致直接判失败；上游失败时下游一律不跑，并让用户决定是否取消。
3. Bug 3：用户说"心情崩溃"，回复只有天气播报。修复：合并节点确定性加一句安抚。
4. Bug 4："用户近期情绪低落"被当长期记忆。修复：Prompt 禁止 + 代码闸门拦截。
"""

import json
import unittest
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from langchain_core.messages import HumanMessage
from sqlalchemy.ext.asyncio import AsyncSession

from agents.common import emotional_comfort_prefix
from agents.state import AgentState
from services import life_service_client as client
from services.memory_noise_filter import is_one_off_log, is_transient_emotional_state
from services.memory_service import strip_superseded_notes
from services.post_chat_memory_extraction import (
    MEMORY_EXTRACTION_SYSTEM_PROMPT,
    _deterministic_correction_target,
    run_post_chat_memory_extraction,
)

USER_ID = uuid.uuid4()


def _settings() -> MagicMock:
    settings = MagicMock()
    settings.database_url = "postgresql+asyncpg://x/y"
    settings.voyageai_api_key = "k"
    settings.voyageai_model = "voyage-3"
    return settings


def _decision_llm(action: str = "ADD", target: str | None = None) -> MagicMock:
    llm = MagicMock()
    llm.ainvoke = AsyncMock(
        return_value=MagicMock(
            content=json.dumps({"action": action, "target_memory_id": target})
        )
    )
    return llm


class SupersededNoteStripTests(unittest.TestCase):
    """旧值绝不能以"备注"形式留在记忆文本里。"""

    def test_parenthesised_note_is_removed(self) -> None:
        self.assertEqual(
            strip_superseded_notes("用户领导是李四（之前张三作废）"),
            "用户领导是李四",
        )
        self.assertEqual(
            strip_superseded_notes("用户领导是李四(原来张三不算数)"),
            "用户领导是李四",
        )

    def test_tail_note_without_brackets_is_removed(self) -> None:
        self.assertEqual(
            strip_superseded_notes("用户领导是李四，之前说的张三作废"),
            "用户领导是李四",
        )

    def test_plain_fact_is_untouched(self) -> None:
        for text in ("用户领导是李四", "用户对花生过敏", "用户住在杭州"):
            self.assertEqual(strip_superseded_notes(text), text)

    def test_note_free_text_with_brackets_is_kept(self) -> None:
        # 括号里不是"作废/之前"这类备注时不能误删（"用户（后端工程师）"）。
        self.assertEqual(
            strip_superseded_notes("用户（后端工程师）住在杭州"),
            "用户（后端工程师）住在杭州",
        )


class CorrectionTargetTests(unittest.TestCase):
    """纠错词 + 同维度 → 必须锁定要失效的旧记录。"""

    def test_correction_with_shared_dimension_targets_old_row(self) -> None:
        old_id = uuid.uuid4()
        similar = [(old_id, "用户领导是张三")]
        self.assertEqual(
            _deterministic_correction_target("用户领导是李四", similar, user_text="不对，我的领导是李四"),
            old_id,
        )

    def test_correction_word_only_in_user_text_still_detected(self) -> None:
        """抽取结果已被改写成客观陈述，纠错词只存在于用户原话里。"""
        old_id = uuid.uuid4()
        similar = [(old_id, "用户的生日是3月5日")]
        self.assertEqual(
            _deterministic_correction_target(
                "用户的生日是4月8日", similar, user_text="记错了，我生日是4月8日",
            ),
            old_id,
        )

    def test_without_correction_word_nothing_is_retired(self) -> None:
        similar = [(uuid.uuid4(), "用户领导是张三")]
        self.assertIsNone(
            _deterministic_correction_target("用户领导是李四", similar, user_text="我的领导是李四")
        )

    def test_different_dimension_is_not_retired(self) -> None:
        similar = [(uuid.uuid4(), "用户住在杭州")]
        self.assertIsNone(
            _deterministic_correction_target("用户领导是李四", similar, user_text="不对，领导是李四")
        )

    def test_ambiguous_candidates_are_left_to_the_llm(self) -> None:
        similar = [
            (uuid.uuid4(), "用户领导是张三"),
            (uuid.uuid4(), "用户的老板是王五"),
        ]
        self.assertIsNone(
            _deterministic_correction_target("用户领导是李四", similar, user_text="不对，领导是李四")
        )


class ExtractionRetiresStaleFactTests(unittest.IsolatedAsyncioTestCase):
    """端到端：旧的"领导是张三"必须被 soft-delete，新文本不能带作废备注。"""

    async def test_stale_fact_is_deactivated_and_note_stripped(self) -> None:
        session = AsyncMock(spec=AsyncSession)
        old_id = uuid.uuid4()

        extraction_llm = MagicMock()
        extraction_llm.ainvoke = AsyncMock(
            return_value=MagicMock(
                content=json.dumps(
                    [{"text": "用户领导是李四（之前张三作废）", "category": "fact"}],
                    ensure_ascii=False,
                )
            )
        )

        with patch(
            "services.post_chat_memory_extraction.find_similar_memories",
            new=AsyncMock(return_value=[(old_id, "用户领导是张三")]),
        ), patch(
            "services.post_chat_memory_extraction.soft_delete_memory",
            new=AsyncMock(return_value=True),
        ) as mock_delete, patch(
            "services.post_chat_memory_extraction.add_memory_tool",
            new=AsyncMock(),
        ) as mock_add:
            await run_post_chat_memory_extraction(
                session,
                _settings(),
                USER_ID,
                [{"role": "user", "content": "不对，我的领导是李四，之前张三那个记错了"}],
                "好的，已更新。",
                extraction_llm=extraction_llm,
                decision_llm=_decision_llm("ADD"),
            )

        mock_delete.assert_awaited_once()
        self.assertEqual(mock_delete.await_args.args[2], old_id)
        self.assertEqual(mock_add.call_args.args[3], "用户领导是李四")


class EmotionalMemoryGateTests(unittest.TestCase):
    """短期情绪不是长期记忆，必须在落库前被拦下。"""

    def test_transient_emotions_are_flagged(self) -> None:
        for text in (
            "用户近期情绪低落",
            "用户今天心情崩溃",
            "用户最近压力很大",
            "用户最近很焦虑",
            "用户这段时间很难过",
        ):
            self.assertTrue(is_transient_emotional_state(text), text)
            self.assertTrue(is_one_off_log(text), text)

    def test_long_term_condition_is_kept(self) -> None:
        for text in (
            "用户长期焦虑，一直在看医生",
            "用户每天都失眠",
        ):
            self.assertFalse(is_one_off_log(text), text)

    def test_non_emotional_facts_pass(self) -> None:
        for text in ("用户喜欢喝燕麦拿铁", "用户在杭州工作"):
            self.assertFalse(is_transient_emotional_state(text), text)

    def test_prompt_forbids_emotional_state(self) -> None:
        self.assertIn("EMOTIONAL STATE — NEVER A LONG-TERM MEMORY", MEMORY_EXTRACTION_SYSTEM_PROMPT)
        self.assertIn("近期压力很大", MEMORY_EXTRACTION_SYSTEM_PROMPT)


class EmotionComfortTests(unittest.IsolatedAsyncioTestCase):
    def test_prefix_only_for_distress(self) -> None:
        self.assertTrue(emotional_comfort_prefix("今天心情崩溃").startswith("抱抱你"))
        self.assertEqual(emotional_comfort_prefix("明天天气怎么样"), "")

    async def test_single_branch_answer_gets_comfort_prefix(self) -> None:
        from agents.orchestrator_node import merge_results_node

        state = AgentState(
            messages=[HumanMessage(content="今天心情崩溃，明天还要出差")],
            agent_results=[{"route": "life", "intent": "查天气", "result": "上海明天：大雨，19~12°C。"}],
            tasks=[{"task_key": "life::查天气"}],
        )
        merged = await merge_results_node(state, llm=MagicMock())
        self.assertTrue(merged["final_answer"].startswith("抱抱你"))
        self.assertIn("大雨", merged["final_answer"], "安抚不能吞掉用户真正问的信息")

    async def test_no_comfort_for_neutral_input(self) -> None:
        from agents.orchestrator_node import merge_results_node

        state = AgentState(
            messages=[HumanMessage(content="明天天气怎么样")],
            agent_results=[{"route": "life", "intent": "查天气", "result": "晴。"}],
            tasks=[{"task_key": "life::查天气"}],
        )
        merged = await merge_results_node(state, llm=MagicMock())
        self.assertEqual(merged["final_answer"], "晴。")


MARS_ROW = {
    "formatted_address": "贵州省黔东南苗族侗族自治州某县火星村",
    "province": "贵州省",
    "city": "黔东南苗族侗族自治州",
    "district": "某县",
    "adcode": "522600",
    "location": "108.0,26.5",
}


class EntityConsistencyTests(unittest.IsolatedAsyncioTestCase):
    """请求的实体 ≠ 返回的实体 → 直接失败，绝不拿别处数据当成功。"""

    def test_district_level_match_is_consistent(self) -> None:
        self.assertFalse(
            client.requested_entity_mismatch(
                "上海", {"province": "上海市", "city": "上海市", "district": "黄浦区"},
            )
        )

    def test_village_hit_is_a_mismatch(self) -> None:
        # 高德把"火星"匹配到贵州的"火星村"：formatted_address 含目标词，但行政区
        # 三级里都没有 → 这不是用户要的地方。
        self.assertTrue(client.requested_entity_mismatch("火星", MARS_ROW))

    async def test_mars_weather_is_rejected_loudly(self) -> None:
        with patch.object(client, "_amap_geocode", return_value=[MARS_ROW]):
            with self.assertRaises(client.LifeServiceError) as ctx:
                await client._resolve_amap_location(
                    MagicMock(amap_api_key="k"), latitude=None, longitude=None, city="火星",
                )
        self.assertIn("查不到", str(ctx.exception))


class UpstreamFailureBlocksDownstreamTests(unittest.IsolatedAsyncioTestCase):
    """上游失败 → 下游不许跑，并让用户决定是否取消（"是否取消写邮件"）。"""

    def _state(self, iteration: int, max_iterations: int) -> dict:
        tasks = [
            {"task_key": "a", "intent": "查询火星明天的天气", "route": "life", "depends_on": []},
            {"task_key": "b", "intent": "根据天气写一封发给张三的邮件", "route": "email", "depends_on": ["a"]},
        ]
        return {
            "tasks": tasks,
            "agent_results": [
                {
                    "task_key": "a",
                    "task_status": "failed",
                    "error": "查不到「火星」这个地方",
                    "attempt": 0,
                }
            ],
            "completed_task_ids": [],
            "plan": {"iteration": iteration},
            "settings": MagicMock(planner_max_iterations=max_iterations),
        }

    async def test_downstream_step_is_skipped_when_budget_exhausted(self) -> None:
        from agents.supervisor_node import supervisor_node

        result = await supervisor_node(self._state(iteration=4, max_iterations=4), MagicMock())

        self.assertEqual(result["supervisor_decision"]["action"], "done")
        # 失败步骤本身保留（最终回复要如实告知失败），下游写邮件步骤必须被摘掉。
        self.assertEqual([t["task_key"] for t in result["tasks"]], ["a"])
        notices = result["blocked_step_notices"]
        self.assertEqual(len(notices), 1)
        self.assertIn("写一封发给张三的邮件", notices[0])
        self.assertIn("是否取消", notices[0])

    async def test_downstream_is_kept_while_replan_budget_remains(self) -> None:
        from agents.supervisor_node import supervisor_node

        result = await supervisor_node(self._state(iteration=0, max_iterations=4), MagicMock())

        self.assertEqual(result["supervisor_decision"]["action"], "replan")
        self.assertTrue(result["force_replan"])

    async def test_notice_is_appended_to_final_answer(self) -> None:
        from agents.orchestrator_node import merge_results_node

        state = AgentState(
            agent_results=[{"route": "life", "intent": "查天气", "result": "查不到火星的天气。"}],
            tasks=[{"task_key": "life::查天气"}],
            blocked_step_notices=[
                "「写邮件」已暂停：上游「查天气」没有拿到数据，为避免用到错误信息，"
                "这一步没有执行。是否取消「写邮件」？"
            ],
        )
        merged = await merge_results_node(state, llm=MagicMock())
        self.assertIn("已暂停的步骤", merged["final_answer"])
        self.assertIn("是否取消", merged["final_answer"])


if __name__ == "__main__":
    unittest.main()
