"""一次性流水账过滤器 —— 长期记忆的写入前闸门。

为什么需要代码闸门：抽取 Prompt 里已经写了 "FORBIDDEN TO SAVE"，但 LLM 仍会
把"明天早上八点改简历""点了杯拿铁"这类一次性事件当事实存进 `user_memories`。
记忆一旦被污染，会通过检索注入污染后续所有对话，且语义去重（余弦 0.92）对
"每次都不一样"的流水账几乎无效。所以必须在落库前做确定性拦截。

判定原则：**重复才记得住，一次只是流水账**。
  - 出现循环/习惯词（每天、每周、经常、总是、习惯…）→ 一定是长期特征，放行；
  - 否则命中以下任一 → 判定为一次性流水账，拦截：
      1. 具体日期/时刻（"明天早上八点"、"今天九点"、"下午3点"、"10月25日"）
      2. 单次交易动作（"抢到了"、"点了杯"、"买了个"、"付了钱"…）
      3. 短暂状态（"今天很忙"、"这周要赶项目"、"好累"…）
"""

import re

# 循环 / 习惯信号：命中即认为是长期特征，不做流水账拦截。
_RECURRENCE_MARKERS = (
    "每天", "每日", "每周", "每星期", "每月", "每年", "每次", "每个工作日",
    "天天", "经常", "常常", "总是", "一向", "历来", "一直以来", "习惯",
    "通常", "平时都", "一般都",
    "every day", "daily", "weekly", "monthly", "yearly", "usually", "always",
    "every week", "every month", "every time",
)

# 具体日期词（配合钟点或单独出现都说明是"某一天的一次性安排"）。
_DATE_WORDS = (
    "今天", "今日", "今晚", "今早", "明天", "明日", "明早", "明晚",
    "后天", "大后天", "昨天", "昨日", "前天", "上周", "下周", "这周", "本周",
    "下个月", "这个月", "月底", "年底",
    "周一", "周二", "周三", "周四", "周五", "周六", "周日", "周天",
    "星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日",
)
# 单日词：出现即说明是"某一天"，即便句子里同时有偏好词也仍是一次性安排。
_SINGLE_DAY_WORDS = (
    "今天", "今日", "今晚", "今早", "明天", "明日", "明早", "明晚",
    "后天", "大后天", "昨天", "昨日", "前天",
)
# 稳定偏好/习惯信号：描述"这个人是什么样的"，而不是"这一天发生了什么"。
_STABLE_MARKERS = (
    "偏好", "喜欢", "习惯", "感兴趣", "通常", "一向", "总是", "经常", "常常",
    "不喜欢", "讨厌", "擅长", "一般会",
)
# 明确钟点：8点 / 八点 / 08:30 / 下午三点。
_CLOCK_RE = re.compile(
    r"\d{1,2}\s*[:：]\s*\d{2}|"
    r"[0-9零一二两三四五六七八九十]{1,3}\s*[点时]"
)
_MONTH_DAY_RE = re.compile(r"\d{1,2}\s*月\s*\d{1,2}\s*[日号]")
# 模糊相对时间：待会儿 / 稍后 / 一会 / 回头。同样指向"某一次"而非长期特征。
_FUZZY_TIME_WORDS = (
    "待会", "待会儿", "一会", "一会儿", "稍后", "过会", "过一会",
    "等会", "晚点", "回头", "马上", "尽快", "过两天", "改天",
)

# 单次交易 / 一次性动作（过去式、单次量词）。刻意不用"买/吃/喝"裸动词，
# 否则"我喜欢喝燕麦拿铁"这类偏好会被误杀。
_TRANSACTION_RE = re.compile(
    r"(抢到|抢了|点了|买了个|买了一个|买了杯|吃了|喝了一杯|喝了杯|"
    r"付了|付款了|充值了|下单了|下了一单|打卡了|领取了|收到了|"
    r"借了|还了|拿了|捡到|抽到|中了|刷了)"
)
# 单次量词 + 消费品：点了杯拿铁 / 买了张票 / 吃了一碗面。
_QUANTITY_RE = re.compile(r"[一两杯碗张件盒瓶个支本袋包]")

# 短暂状态：今天很忙 / 好累 / 这周要赶项目 / 近期情绪低落。
# 情绪词单独成组：真实的 badcase 是"心情崩溃"被抽成"用户近期情绪低落"，
# 存进长期记忆后每轮都被检索注入，助手会永远把用户当成"正在崩溃的人"。
_TRANSIENT_RE = re.compile(
    r"(很忙|好忙|有点忙|太忙|好累|很累|有点累|累了|烦死|有点烦|心情不好|"
    r"赶项目|赶due|加班到|熬夜了|没睡好)"
)
# 情绪状态：一次性的心情不等于这个人的长期特征。只有带循环/习惯词
# （"我最近每天都焦虑"）才放行。
_EMOTIONAL_STATE_RE = re.compile(
    r"(崩溃|快崩溃|要崩溃|情绪低落|情绪很差|心情很差|心情低落|沮丧|焦虑|"
    r"抑郁|心烦|烦躁|难过|伤心|想哭|哭了|失落|emo|压力大|压力很大|"
    r"扛不住|撑不住|受不了|心态崩了|不开心|心情复杂|心累|痛苦|憋屈)"
)
# 长期状态词：把"一次性的心情"和"这个人的稳定状况"区分开。
# "用户长期焦虑，一直在看医生" 是可保留的背景事实；"用户近期情绪低落" 不是。
_LONG_TERM_STATE_WORDS = (
    "长期", "一直", "常年", "多年", "从小就", "一直以来", "持续性",
    "确诊", "病史", "看医生", "在服药", "心理咨询",
)


def _has_recurrence(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _RECURRENCE_MARKERS)


# 长期习惯："爱玩手机""喜欢打游戏""爱刷短视频"。它们描述的是"这个人平时什么样"，
# 属于偏好(preference)，不是某一天的流水账(episodic)。
_HABIT_RE = re.compile(
    r"爱玩|爱刷|爱看|爱吃|爱喝|爱用|喜欢玩|喜欢刷|喜欢看|喜欢打|喜欢喝|喜欢穿|"
    r"沉迷|上瘾|离不开"
)


def describe_habit(text: str) -> bool:
    """这条记忆是否在描述长期个人习惯（而不是单次事件）？"""
    return bool(_HABIT_RE.search(text or ""))


def is_transient_emotional_state(text: str) -> bool:
    """这条记忆是否在记录一次性的情绪状态（而不是长期稳定的偏好/事实）？

    "用户近期情绪低落"、"我最近压力很大" 是一次性状态：存进长期记忆后会被
    每轮检索注入，助手会永远把用户当成"正在崩溃的人"，既冒犯又失真。
    带循环/长期词（"我长期焦虑，一直在看医生"）属于稳定特征，放行。
    """
    clean = (text or "").strip()
    if not clean:
        return False
    if not _EMOTIONAL_STATE_RE.search(clean):
        return False
    if _has_recurrence(clean):
        return False
    return not any(word in clean for word in _LONG_TERM_STATE_WORDS)


def correct_habit_category(text: str, category: str) -> str:
    """把被 LLM 误判成流水账的长期习惯改回 preference。

    "爱玩手机" 被归为 episodic 会让检索把它当"昨天发生的一件事"，几轮后就被
    丢弃；它实际是长期偏好，必须留在 preference 里持续生效。
    """
    if (category or "").strip().lower() in {"episodic", "fact"} and describe_habit(text):
        return "preference"
    return category


# 第一人称显式偏好："我最喜欢喝拿铁""我更爱美式""我今天最喜欢喝拿铁"。
# 主语和好恶词之间允许夹修饰语（"今天""其实"），否则"我今天最喜欢…"会被漏掉。
_EXPLICIT_PREFERENCE_RE = re.compile(
    r"我[^。！？!?\n，,；;]{0,8}?(?:喜欢|喜爱|偏好|偏爱|讨厌|中意|钟爱)"
)
# 说别人（他/她/同事）的喜好不是用户对自己的定义。
_THIRD_PERSON_RE = re.compile(r"他|她|他们|她们|同事|朋友|室友")

# 带强调词的偏好表态，不区分人称：记忆抽取会把用户原话改写成"用户最喜欢…"，
# 这一形式同样是一次明确表态，必须能覆盖同领域的旧偏好。
_STRONG_PREFERENCE_RE = re.compile(
    r"(?:最|更|特别|非常|比较|真的|其实|现在)"
    r"(?:喜欢|喜爱|偏好|偏爱|爱|讨厌|中意|钟爱)"
)


def is_explicit_preference(text: str) -> bool:
    """用户是否在第一人称明确表达了好恶。"""
    match = _EXPLICIT_PREFERENCE_RE.search(text or "")
    if match is None:
        return False
    return not _THIRD_PERSON_RE.search(match.group(0))


def is_strong_preference(text: str) -> bool:
    """是否用"最/更/特别"等强调词表达了偏好（含第三人称改写形式）。"""
    return bool(_STRONG_PREFERENCE_RE.search(text or ""))


def is_one_off_log(text: str) -> bool:
    """这条候选记忆是不是一次性流水账（不该进长期记忆）？"""
    clean = (text or "").strip()
    if not clean:
        return False
    # 长期特征优先：只要带循环/习惯词，就当成值得记住的稳定事实。
    if _has_recurrence(clean):
        return False
    # 显式第一人称偏好同样是长期特征：即便句子里出现"今天/明天"，
    # "我今天最喜欢喝拿铁"说的仍是用户的口味，不能当流水账丢掉。
    if is_explicit_preference(clean):
        return False

    if _MONTH_DAY_RE.search(clean):
        return True
    # "待会儿/稍后"这类相对时间本身即指某一次，和"今天/明天"同等对待。
    has_date = any(word in clean for word in _DATE_WORDS) or any(
        word in clean for word in _FUZZY_TIME_WORDS
    )
    has_clock = bool(_CLOCK_RE.search(clean))
    has_single_day = any(word in clean for word in _SINGLE_DAY_WORDS)
    has_stable = any(word in clean for word in _STABLE_MARKERS)

    # 单日词（今天/明天…）优先：即使是"明天想…"的偏好，也只在明天成立。
    if has_single_day:
        return True
    # 明确偏好/习惯（"偏好早上8点处理工作""喜欢晚上10点听音乐"）是稳定特征，
    # 带钟点也不算流水账。
    if has_stable:
        return False
    # 无偏好/习惯信号的日期或钟点 → 某一天的一次性安排。
    if has_date or has_clock:
        return True
    if _TRANSACTION_RE.search(clean):
        return True
    if _QUANTITY_RE.search(clean) and any(v in clean for v in ("点了", "买了", "吃了", "喝了")):
        return True
    if _TRANSIENT_RE.search(clean):
        return True
    if is_transient_emotional_state(clean):
        return True
    return False
