"""任务语义分类：把任务归入 工作/生活/学习/其他，驱动前端项目过滤按钮。

Planner Prompt 会要求 LLM 直接输出 `project` 枚举；这里的关键词规则是
LLM 未返回或返回非法值时的规则兜底，保证任何任务都落在四个枚举之一。
"""

from lib.enums import TaskProject

# (关键词, 项目) — 按标题语义分类。命中任一组内任意关键词即归入对应项目。
_PROJECT_KEYWORD_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        (
            "周报",
            "日报",
            "报表",
            "邮件",
            "简历",
            "面试",
            "开会",
            "会议",
            "汇报",
            "项目",
            "方案",
            "PPT",
            "客户",
            "报销",
            "工作",
            "需求",
            "代码",
            "产品",
            "绩效",
            "offer",
            "招聘",
            "合同",
            "述职",
        ),
        TaskProject.WORK.value,
    ),
    (
        (
            "考试",
            "复习",
            "上课",
            "课程",
            "读书",
            "看书",
            "学习",
            "练习",
            "背单词",
            "背诵",
            "刷题",
            "考证",
            "论文",
            "作业",
            "听讲座",
            "培训",
            "自习",
        ),
        TaskProject.STUDY.value,
    ),
    (
        (
            "买",
            "缴费",
            "电费",
            "水费",
            "煤气",
            "房租",
            "买菜",
            "做饭",
            "洗",
            "运动",
            "锻炼",
            "健身",
            "跑步",
            "体检",
            "看病",
            "医院",
            "睡觉",
            "休息",
            "旅行",
            "收拾",
            "打扫",
            "维修",
            "拿快递",
            "取快递",
            "购物",
        ),
        TaskProject.LIFE.value,
    ),
)


def classify_task_project(intent: str, fallback: str = TaskProject.LIFE.value) -> str:
    """按任务语义归到 工作/生活/学习/其他；判断不出默认 生活。"""
    text = (intent or "").lower()
    for keywords, project in _PROJECT_KEYWORD_RULES:
        if any(kw.lower() in text for kw in keywords):
            return project
    return fallback


def validate_task_project(value: object, fallback: str = TaskProject.LIFE.value) -> str:
    """归一化 LLM 返回的 project：只接受四个枚举值，否则回退默认。"""
    if value is None:
        return fallback
    candidate = str(value).strip()
    if candidate in {project.value for project in TaskProject}:
        return candidate
    return fallback
