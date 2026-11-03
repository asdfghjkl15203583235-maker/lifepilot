"""任务来源分类：user（待办，进任务概览）vs system（内部，只进动态/记忆）。

规则用成对关键词减少误伤："记录""写入"等动作必须伴随"记忆/偏好/习惯/喜好"等对象
才算 system；"记录会议安排"这类用户动作不会被误判。
"""

# (动作词, 记忆对象词)；second 为空串表示仅靠 first 命中即可。
_SYSTEM_SOURCE_PAIRS = (
    ("记录", "记忆"),
    ("记录", "偏好"),
    ("记录", "习惯"),
    ("记录", "喜好"),
    ("记录", "喜欢"),
    ("记录", "习惯性"),
    ("记住", "记忆"),
    ("记住", "偏好"),
    ("记住", "喜欢"),
    ("记住", "不要"),
    ("记住我", ""),
    ("更新记忆", ""),
    ("写入记忆", ""),
    ("保存到记忆", ""),
    ("偏好", "喝"),
    ("偏好", "喜欢"),
    ("记忆", "保存"),
)


def classify_task_source(intent: str) -> str:
    """根据意图把任务归入 user 或 system；命中任一"动作词+记忆对象"组合 -> system。"""
    text = intent or ""
    for first, second in _SYSTEM_SOURCE_PAIRS:
        if first in text and (not second or second in text):
            return "system"
    return "user"