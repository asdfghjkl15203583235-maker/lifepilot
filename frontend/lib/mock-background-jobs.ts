export const JOB_STATUS_ACTIVE = "active" as const;
export const JOB_STATUS_PAUSED = "paused" as const;
export const JOB_STATUS_ENDED = "ended" as const;

export type JobStatus = typeof JOB_STATUS_ACTIVE | typeof JOB_STATUS_PAUSED | typeof JOB_STATUS_ENDED;

export const JOB_STATUS_LABEL: Record<JobStatus, string> = {
  [JOB_STATUS_ACTIVE]: "运行中",
  [JOB_STATUS_PAUSED]: "已暂停",
  [JOB_STATUS_ENDED]: "已结束",
};

export const ACTION_TYPE_EMAIL = "email" as const;
export const ACTION_TYPE_CALENDAR = "calendar" as const;
export const ACTION_TYPE_NOTION = "notion" as const;
export const ACTION_TYPE_CUSTOM = "custom" as const;

export type ActionType =
  | typeof ACTION_TYPE_EMAIL
  | typeof ACTION_TYPE_CALENDAR
  | typeof ACTION_TYPE_NOTION
  | typeof ACTION_TYPE_CUSTOM;

export const ACTION_TYPE_LABEL: Record<ActionType, string> = {
  [ACTION_TYPE_EMAIL]: "邮件",
  [ACTION_TYPE_CALENDAR]: "日历",
  [ACTION_TYPE_NOTION]: "Notion",
  [ACTION_TYPE_CUSTOM]: "自定义",
};

export const RUN_OUTCOME_SUCCESS = "success" as const;
export const RUN_OUTCOME_FAILED = "failed" as const;
export const RUN_OUTCOME_SKIPPED = "skipped" as const;

export type RunOutcome =
  | typeof RUN_OUTCOME_SUCCESS
  | typeof RUN_OUTCOME_FAILED
  | typeof RUN_OUTCOME_SKIPPED;

export const RUN_OUTCOME_LABEL: Record<RunOutcome, string> = {
  [RUN_OUTCOME_SUCCESS]: "成功",
  [RUN_OUTCOME_FAILED]: "失败",
  [RUN_OUTCOME_SKIPPED]: "已跳过",
};

export const TRACE_STEP_INTENT = "intent" as const;
export const TRACE_STEP_TOOL = "tool" as const;
export const TRACE_STEP_APPROVAL = "approval" as const;
export const TRACE_STEP_EMIT = "emit" as const;

export type TraceStepKind =
  | typeof TRACE_STEP_INTENT
  | typeof TRACE_STEP_TOOL
  | typeof TRACE_STEP_APPROVAL
  | typeof TRACE_STEP_EMIT;

export interface ScheduleAction {
  readonly actionType: ActionType;
  readonly label: string;
}

export interface BackgroundJob {
  readonly id: string;
  readonly title: string;
  readonly instruction: string;
  readonly scheduleDescription: string;
  readonly timezoneLabel: string;
  readonly actions: readonly ScheduleAction[];
  readonly status: JobStatus;
  readonly nextRunIso: string | null;
  /** Inclusive: schedule may first run at or after this instant. */
  readonly scheduleStartsAtIso?: string;
  /** Exclusive: no run at or after this instant. */
  readonly scheduleEndsAtIso?: string;
  readonly createdAtIso: string;
  readonly endedAtIso?: string;
}

export interface AgentTraceStep {
  readonly id: string;
  readonly agentLabel: string;
  readonly stepKind: TraceStepKind;
  readonly title: string;
  readonly detail: string;
  readonly atIso: string;
}

export interface JobRun {
  readonly id: string;
  readonly jobId: string;
  readonly jobTitle: string;
  readonly startedAtIso: string;
  readonly finishedAtIso: string;
  readonly outcome: RunOutcome;
  readonly detail: string;
  readonly durationMs: number;
  readonly traces: readonly AgentTraceStep[];
}

export const MOCK_BACKGROUND_JOBS: readonly BackgroundJob[] = [
  {
    id: "job-email-digest",
    title: "早间合作伙伴邮件",
    instruction:
      "每个工作日 10:00 向 partner@mail.com 发送简短状态邮件，并附上明天的日历冲突。",
    scheduleDescription: "工作日 10:00",
    timezoneLabel: "America/Los_Angeles",
    actions: [
      { actionType: ACTION_TYPE_EMAIL, label: "发送邮件给 partner@mail.com" },
      { actionType: ACTION_TYPE_CALENDAR, label: "附上次日冲突" },
    ],
    status: JOB_STATUS_ACTIVE,
    nextRunIso: "2026-04-24T17:00:00.000Z",
    createdAtIso: "2026-04-01T12:00:00.000Z",
  },
  {
    id: "job-weekly-rollups",
    title: "周度指标汇总",
    instruction: "每周一将收件箱和日历指标汇总写入 Notion 页面。",
    scheduleDescription: "每周一 9:00",
    timezoneLabel: "America/Los_Angeles",
    actions: [
      { actionType: ACTION_TYPE_EMAIL, label: "发送摘要给领导@" },
      { actionType: ACTION_TYPE_NOTION, label: "更新指标数据库" },
    ],
    status: JOB_STATUS_ACTIVE,
    nextRunIso: "2026-04-28T16:00:00.000Z",
    createdAtIso: "2026-03-15T09:30:00.000Z",
  },
  {
    id: "job-meeting-prep",
    title: "会议准备简报",
    instruction: "在管理层站会前起草一页简报。",
    scheduleDescription: "工作日 8:30",
    timezoneLabel: "America/Los_Angeles",
    actions: [{ actionType: ACTION_TYPE_CALENDAR, label: "附上站会议程" }],
    status: JOB_STATUS_PAUSED,
    nextRunIso: "2026-04-23T15:30:00.000Z",
    createdAtIso: "2026-02-10T14:00:00.000Z",
  },
  {
    id: "job-q1-automation",
    title: "Q1 投资人自动化",
    instruction: "每周给投资人的资料包——已被新的 CRM 工作流取代。",
    scheduleDescription: "已结束",
    timezoneLabel: "America/Los_Angeles",
    actions: [
      { actionType: ACTION_TYPE_EMAIL, label: "发送邮件给投资人@" },
      { actionType: ACTION_TYPE_NOTION, label: "抓取渠道快照" },
    ],
    status: JOB_STATUS_ENDED,
    nextRunIso: null,
    createdAtIso: "2025-12-01T09:00:00.000Z",
    endedAtIso: "2026-03-31T23:59:59.000Z",
  },
  {
    id: "job-beta-digest",
    title: "Beta 试点摘要",
    instruction: "为 Beta 试点成员提供每日摘要（试点已结束）。",
    scheduleDescription: "已结束",
    timezoneLabel: "America/Los_Angeles",
    actions: [
      { actionType: ACTION_TYPE_EMAIL, label: "发送到试点收件箱" },
      { actionType: ACTION_TYPE_CUSTOM, label: "汇总反馈" },
    ],
    status: JOB_STATUS_ENDED,
    nextRunIso: null,
    createdAtIso: "2026-01-05T08:00:00.000Z",
    endedAtIso: "2026-04-15T18:00:00.000Z",
  },
];

const TRACE_RUN_501: readonly AgentTraceStep[] = [
  {
    id: "t501-a",
    agentLabel: "规划器",
    stepKind: TRACE_STEP_INTENT,
    title: "已完成意图分类",
    detail: "定时任务 job-email-digest · intent = 外发状态邮件",
    atIso: "2026-04-23T17:00:02.100Z",
  },
  {
    id: "t501-b",
    agentLabel: "日历代理",
    stepKind: TRACE_STEP_TOOL,
    title: "已获取冲突",
    detail: "calendar.list_events(day=2026-04-25) → 2 个阻塞占位",
    atIso: "2026-04-23T17:00:05.400Z",
  },
  {
    id: "t501-c",
    agentLabel: "草稿代理",
    stepKind: TRACE_STEP_TOOL,
    title: "已起草正文",
    detail: "compose_email(template=status_plain, recipients=[partner@mail.com])",
    atIso: "2026-04-23T17:00:11.200Z",
  },
  {
    id: "t501-d",
    agentLabel: "审批门",
    stepKind: TRACE_STEP_APPROVAL,
    title: "已加入用户审批队列",
    detail: "发送前等待审批节点确认",
    atIso: "2026-04-23T17:00:14.800Z",
  },
  {
    id: "t501-e",
    agentLabel: "规划器",
    stepKind: TRACE_STEP_EMIT,
    title: "运行完成",
    detail: "成功 · artifacts: draft_id=em_9dx2",
    atIso: "2026-04-23T17:00:18.450Z",
  },
];

const TRACE_RUN_500: readonly AgentTraceStep[] = [
  {
    id: "t500-a",
    agentLabel: "规划器",
    stepKind: TRACE_STEP_INTENT,
    title: "已完成意图分类",
    detail: "定时任务 job-email-digest",
    atIso: "2026-04-22T17:00:01.800Z",
  },
  {
    id: "t500-b",
    agentLabel: "Gmail 工具",
    stepKind: TRACE_STEP_TOOL,
    title: "已发送邮件",
    detail: "gmail.send(thread=…, approved=true)",
    atIso: "2026-04-22T17:00:12.100Z",
  },
  {
    id: "t500-c",
    agentLabel: "规划器",
    stepKind: TRACE_STEP_EMIT,
    title: "运行完成",
    detail: "成功",
    atIso: "2026-04-22T17:00:14.200Z",
  },
];

const TRACE_RUN_499: readonly AgentTraceStep[] = [
  {
    id: "t499-a",
    agentLabel: "规划器",
    stepKind: TRACE_STEP_INTENT,
    title: "已完成意图分类",
    detail: "定时任务 job-weekly-rollups",
    atIso: "2026-04-21T16:00:04.000Z",
  },
  {
    id: "t499-b",
    agentLabel: "Notion MCP",
    stepKind: TRACE_STEP_TOOL,
    title: "更新被阻断",
    detail: "notion.pages.update 30 秒超时 — 熔断已触发",
    atIso: "2026-04-21T16:02:34.000Z",
  },
  {
    id: "t499-c",
    agentLabel: "规划器",
    stepKind: TRACE_STEP_EMIT,
    title: "运行完成",
    detail: "失败 · retry_policy=next_window",
    atIso: "2026-04-21T16:02:41.900Z",
  },
];

const TRACE_RUN_498: readonly AgentTraceStep[] = [
  {
    id: "t498-a",
    agentLabel: "规划器",
    stepKind: TRACE_STEP_INTENT,
    title: "已完成意图分类",
    detail: "定时任务 job-email-digest",
    atIso: "2026-04-21T17:00:02.000Z",
  },
  {
    id: "t498-b",
    agentLabel: "策略守卫",
    stepKind: TRACE_STEP_APPROVAL,
    title: "假期暂停",
    detail: "calendar.is_holidays=true → 外发邮件已抑制",
    atIso: "2026-04-21T17:00:03.100Z",
  },
  {
    id: "t498-c",
    agentLabel: "规划器",
    stepKind: TRACE_STEP_EMIT,
    title: "运行已跳过",
    detail: "结果：已跳过",
    atIso: "2026-04-21T17:00:03.500Z",
  },
];

const TRACE_RUN_497: readonly AgentTraceStep[] = [
  {
    id: "t497-a",
    agentLabel: "规划器",
    stepKind: TRACE_STEP_INTENT,
    title: "已完成意图分类",
    detail: "定时任务 job-meeting-prep",
    atIso: "2026-04-19T15:30:01.000Z",
  },
  {
    id: "t497-b",
    agentLabel: "日历代理",
    stepKind: TRACE_STEP_TOOL,
    title: "已关联简报",
    detail: "calendar.attach_notes(event_id=standup, doc=brief_md)",
    atIso: "2026-04-19T15:30:44.000Z",
  },
  {
    id: "t497-c",
    agentLabel: "规划器",
    stepKind: TRACE_STEP_EMIT,
    title: "运行完成",
    detail: "成功",
    atIso: "2026-04-19T15:31:02.400Z",
  },
];

export const MOCK_JOB_RUNS: readonly JobRun[] = [
  {
    id: "run-501",
    jobId: "job-email-digest",
    jobTitle: "早间合作伙伴邮件",
    startedAtIso: "2026-04-23T17:00:02.100Z",
    finishedAtIso: "2026-04-23T17:00:18.450Z",
    outcome: RUN_OUTCOME_SUCCESS,
    detail: "草稿已创建并进入审批队列（2 位收件人）。",
    durationMs: 16350,
    traces: TRACE_RUN_501,
  },
  {
    id: "run-500",
    jobId: "job-email-digest",
    jobTitle: "早间合作伙伴邮件",
    startedAtIso: "2026-04-22T17:00:01.800Z",
    finishedAtIso: "2026-04-22T17:00:14.200Z",
    outcome: RUN_OUTCOME_SUCCESS,
    detail: "审批后已发送。",
    durationMs: 12400,
    traces: TRACE_RUN_500,
  },
  {
    id: "run-499",
    jobId: "job-weekly-rollups",
    jobTitle: "周度指标汇总",
    startedAtIso: "2026-04-21T16:00:04.000Z",
    finishedAtIso: "2026-04-21T16:02:41.900Z",
    outcome: RUN_OUTCOME_FAILED,
    detail: "Notion MCP 30 秒超时 — 已安排重试。",
    durationMs: 157900,
    traces: TRACE_RUN_499,
  },
  {
    id: "run-498",
    jobId: "job-email-digest",
    jobTitle: "早间合作伙伴邮件",
    startedAtIso: "2026-04-21T17:00:02.000Z",
    finishedAtIso: "2026-04-21T17:00:03.500Z",
    outcome: RUN_OUTCOME_SKIPPED,
    detail: "假期日历阻止了自动发送。",
    durationMs: 1500,
    traces: TRACE_RUN_498,
  },
  {
    id: "run-497",
    jobId: "job-meeting-prep",
    jobTitle: "会议准备简报",
    startedAtIso: "2026-04-19T15:30:01.000Z",
    finishedAtIso: "2026-04-19T15:31:02.400Z",
    outcome: RUN_OUTCOME_SUCCESS,
    detail: "简报已附加到日历事件备注。",
    durationMs: 61400,
    traces: TRACE_RUN_497,
  },
];

export function countJobsByStatus(jobs: readonly BackgroundJob[]): Record<JobStatus, number> {
  return jobs.reduce(
    (accumulator, job) => {
      accumulator[job.status] += 1;
      return accumulator;
    },
    { [JOB_STATUS_ACTIVE]: 0, [JOB_STATUS_PAUSED]: 0, [JOB_STATUS_ENDED]: 0 },
  );
}

export function isOngoingSchedule(job: BackgroundJob): boolean {
  return job.status === JOB_STATUS_ACTIVE || job.status === JOB_STATUS_PAUSED;
}

export function partitionSchedulesByLifecycle(jobs: readonly BackgroundJob[]): {
  ongoing: BackgroundJob[];
  ended: BackgroundJob[];
} {
  const ongoing: BackgroundJob[] = [];
  const ended: BackgroundJob[] = [];
  for (const job of jobs) {
    if (isOngoingSchedule(job)) {
      ongoing.push(job);
    } else if (job.status === JOB_STATUS_ENDED) {
      ended.push(job);
    }
  }
  return { ongoing, ended };
}
