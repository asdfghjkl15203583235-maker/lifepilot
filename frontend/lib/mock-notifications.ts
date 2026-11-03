export const NOTIFICATION_CATEGORY_EMAIL = "email" as const;
export const NOTIFICATION_CATEGORY_CALENDAR = "calendar" as const;
export const NOTIFICATION_CATEGORY_AGENT = "agent" as const;
export const NOTIFICATION_CATEGORY_SYSTEM = "system" as const;
export const NOTIFICATION_CATEGORY_BACKGROUND_JOB = "background_job" as const;
export const NOTIFICATION_CATEGORY_REMINDER = "reminder" as const;

export type NotificationCategory =
  | typeof NOTIFICATION_CATEGORY_EMAIL
  | typeof NOTIFICATION_CATEGORY_CALENDAR
  | typeof NOTIFICATION_CATEGORY_AGENT
  | typeof NOTIFICATION_CATEGORY_SYSTEM
  | typeof NOTIFICATION_CATEGORY_BACKGROUND_JOB
  | typeof NOTIFICATION_CATEGORY_REMINDER;

export const NOTIFICATION_CATEGORY_LABEL: Record<NotificationCategory, string> = {
  [NOTIFICATION_CATEGORY_EMAIL]: "邮件",
  [NOTIFICATION_CATEGORY_CALENDAR]: "日历",
  [NOTIFICATION_CATEGORY_AGENT]: "代理",
  [NOTIFICATION_CATEGORY_SYSTEM]: "系统",
  [NOTIFICATION_CATEGORY_BACKGROUND_JOB]: "定时任务",
  [NOTIFICATION_CATEGORY_REMINDER]: "任务提醒",
};

export interface NotificationItem {
  readonly id: string;
  readonly title: string;
  readonly body: string;
  readonly category: NotificationCategory;
  readonly createdAtIso: string;
  readonly isRead: boolean;
  /** Linked reminder (for task-deadline toasts); set by reminder-origin notifications. */
  readonly reminderId?: string | null;
  readonly taskId?: string | null;
}

export const MOCK_NOTIFICATIONS: readonly NotificationItem[] = [
  {
    id: "ntf-1",
    title: "草稿待批准",
    body: "早间合作伙伴邮件已就绪，确认后可发送至 partner@mail.com。",
    category: NOTIFICATION_CATEGORY_AGENT,
    createdAtIso: "2026-04-23T16:42:00.000Z",
    isRead: false,
  },
  {
    id: "ntf-2",
    title: "会议时间已调整",
    body: "设计同步改至下午 3:00 — LifePilot 已更新你的日历。",
    category: NOTIFICATION_CATEGORY_CALENDAR,
    createdAtIso: "2026-04-23T14:05:00.000Z",
    isRead: false,
  },
  {
    id: "ntf-3",
    title: "周度汇总已完成",
    body: "指标快照已写入你的 Notion 数据库。",
    category: NOTIFICATION_CATEGORY_AGENT,
    createdAtIso: "2026-04-23T09:12:00.000Z",
    isRead: false,
  },
  {
    id: "ntf-4",
    title: "Gmail 已连接",
    body: "收件箱已关联 — 你可以询问邮件线程和草稿。",
    category: NOTIFICATION_CATEGORY_SYSTEM,
    createdAtIso: "2026-04-22T11:00:00.000Z",
    isRead: true,
  },
  {
    id: "ntf-5",
    title: "低优先级摘要已归档",
    body: "安全简报已按你的规则归档。",
    category: NOTIFICATION_CATEGORY_EMAIL,
    createdAtIso: "2026-04-21T08:30:00.000Z",
    isRead: true,
  },
];

export function countUnreadNotifications(items: readonly NotificationItem[]): number {
  return items.filter((item) => !item.isRead).length;
}
