"use client";

import { format, formatDistanceToNow } from "date-fns";
import { zhCN } from "date-fns/locale";
import type { ReactNode } from "react";

import {
  NOTIFICATION_CATEGORY_AGENT,
  NOTIFICATION_CATEGORY_BACKGROUND_JOB,
  NOTIFICATION_CATEGORY_CALENDAR,
  NOTIFICATION_CATEGORY_EMAIL,
  NOTIFICATION_CATEGORY_LABEL,
  NOTIFICATION_CATEGORY_REMINDER,
  NOTIFICATION_CATEGORY_SYSTEM,
  type NotificationCategory,
  type NotificationItem,
} from "@/lib/mock-notifications";

function IconEnvelope({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.75} aria-hidden>
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M3 8l7.89 5.26a2 2 0 002.22 0L21 8M5 19h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z"
      />
    </svg>
  );
}

function IconCalendarSmall({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.75} aria-hidden>
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M8 7V3m8 4V3m-9 8h10M5 21h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z"
      />
    </svg>
  );
}

function IconSparkles({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.75} aria-hidden>
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M5 3v4M3 5h4M6 17v4m-2-2h4m5-16l2.286 6.857L21 12l-5.714 2.143L13 21l-2.286-6.857L5 12l5.714-2.143L13 3z"
      />
    </svg>
  );
}

function IconCog({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.75} aria-hidden>
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z"
      />
      <path strokeLinecap="round" strokeLinejoin="round" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
    </svg>
  );
}

function IconLayersSmall({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.75} aria-hidden>
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M12 3l9 5-9 5-9-5 9-5zM3 12l9 5 9-5M3 17l9 5 9-5"
      />
    </svg>
  );
}

function IconBellSmall({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.75} aria-hidden>
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M15 17h5l-1.405-1.405A2.032 2.032 0 0118 14.158V11a6.002 6.002 0 00-4-5.659V5a2 2 0 10-4 0v.341C7.67 6.165 6 8.388 6 11v3.159c0 .538-.214 1.055-.595 1.436L4 17h5m6 0v1a3 3 0 11-6 0v-1m6 0H9"
      />
    </svg>
  );
}

function categoryIcon(category: NotificationCategory): ReactNode {
  const common = "h-4 w-4 shrink-0";
  switch (category) {
    case NOTIFICATION_CATEGORY_EMAIL:
      return <IconEnvelope className={common} />;
    case NOTIFICATION_CATEGORY_CALENDAR:
      return <IconCalendarSmall className={common} />;
    case NOTIFICATION_CATEGORY_AGENT:
      return <IconSparkles className={common} />;
    case NOTIFICATION_CATEGORY_SYSTEM:
      return <IconCog className={common} />;
    case NOTIFICATION_CATEGORY_BACKGROUND_JOB:
      return <IconLayersSmall className={common} />;
    case NOTIFICATION_CATEGORY_REMINDER:
      return <IconBellSmall className={common} />;
    default:
      return null;
  }
}

const CATEGORY_ICON_WRAP: Record<NotificationCategory, string> = {
  [NOTIFICATION_CATEGORY_EMAIL]: "bg-sky-500/20 text-sky-100 ring-sky-400/25",
  [NOTIFICATION_CATEGORY_CALENDAR]: "bg-violet-500/20 text-violet-100 ring-violet-400/25",
  [NOTIFICATION_CATEGORY_AGENT]: "bg-emerald-500/20 text-emerald-100 ring-emerald-400/25",
  [NOTIFICATION_CATEGORY_SYSTEM]: "bg-amber-500/20 text-amber-100 ring-amber-400/25",
  [NOTIFICATION_CATEGORY_BACKGROUND_JOB]: "bg-fuchsia-500/20 text-fuchsia-100 ring-fuchsia-400/25",
  [NOTIFICATION_CATEGORY_REMINDER]: "bg-rose-500/20 text-rose-100 ring-rose-400/25",
};

interface NotificationsDashboardProps {
  readonly items: readonly NotificationItem[];
  readonly isLoading?: boolean;
  readonly loadError?: string | null;
  readonly onMarkItemRead?: (id: string) => void;
  readonly onMarkAllRead?: () => void;
  readonly onCreateTask?: (notificationId: string) => void;
  readonly onOpenTask?: (taskId: string) => void;
}

export function NotificationsDashboard({
  items,
  isLoading = false,
  loadError = null,
  onMarkItemRead,
  onMarkAllRead,
  onCreateTask,
  onOpenTask,
}: NotificationsDashboardProps) {
  const unread = items.filter((item) => !item.isRead);
  const read = items.filter((item) => item.isRead);

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-10 overflow-y-auto overflow-x-hidden pb-10 pt-1 [scrollbar-gutter:stable]">
      {loadError ? (
        <div
          className="shrink-0 rounded-xl border border-rose-400/30 bg-rose-500/10 px-4 py-3 text-[13px] text-rose-100/95"
          role="alert"
        >
          {loadError}
        </div>
      ) : null}

      <div
        className="shrink-0 rounded-xl border border-violet-400/25 bg-violet-500/10 px-4 py-3 text-[13px] text-white/85"
        role="status"
      >
        <p className="font-medium text-white">收件箱</p>
        <p className="mt-1 text-white/65">
          任务到期提醒、定时任务完成或失败都会显示在这里。点击条目可标记为已读。
        </p>
        {onMarkAllRead && unread.length > 0 ? (
          <button
            type="button"
            onClick={() => onMarkAllRead()}
            className="mt-3 rounded-lg border border-white/20 bg-white/10 px-3 py-1.5 text-[12px] font-medium text-white/90 transition hover:bg-white/15"
          >
            全部标为已读
          </button>
        ) : null}
      </div>

      {isLoading ? (
        <p className="shrink-0 text-[13px] text-white/50">正在加载通知…</p>
      ) : null}

      {!isLoading && unread.length > 0 ? (
        <section aria-labelledby="notifications-unread-heading" className="shrink-0 space-y-3">
          <h2 id="notifications-unread-heading" className="text-sm font-semibold text-white/90">
            未读 ({unread.length})
          </h2>
          <ul className="flex flex-col gap-3">
            {unread.map((item) => (
              <li key={item.id}>
                <NotificationRow
                  item={item}
                  onMarkRead={onMarkItemRead}
                  onCreateTask={onCreateTask}
                  onOpenTask={onOpenTask}
                />
              </li>
            ))}
          </ul>
        </section>
      ) : !isLoading && items.length > 0 ? (
        <p className="shrink-0 rounded-xl border border-white/10 bg-black/25 px-4 py-6 text-center text-[13px] text-white/50">
          没有未读通知。
        </p>
      ) : null}

      <section aria-labelledby="notifications-read-heading" className="shrink-0 space-y-3 border-t border-white/10 pt-8">
        <h2 id="notifications-read-heading" className="text-sm font-semibold text-white/90">
          较早
        </h2>
        {read.length === 0 ? (
          <p className="rounded-xl border border-white/10 bg-black/25 px-4 py-8 text-center text-[13px] text-white/50">
            {items.length === 0 && !isLoading ? "还没有通知。" : "没有已归档通知。"}
          </p>
        ) : (
          <ul className="flex flex-col gap-3">
            {read.map((item) => (
              <li key={item.id}>
                <NotificationRow
                  item={item}
                  onMarkRead={onMarkItemRead}
                  onCreateTask={onCreateTask}
                  onOpenTask={onOpenTask}
                />
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

function NotificationRow({
  item,
  onMarkRead,
  onCreateTask,
  onOpenTask,
}: {
  readonly item: NotificationItem;
  readonly onMarkRead?: (id: string) => void;
  readonly onCreateTask?: (notificationId: string) => void;
  readonly onOpenTask?: (taskId: string) => void;
}) {
  const created = new Date(item.createdAtIso);
  const relative = formatDistanceToNow(created, { addSuffix: true, locale: zhCN });
  const isActionable = Boolean(onMarkRead) && !item.isRead;
  const linkedTaskId = item.taskId ?? null;
  const isEmailItem = item.category === NOTIFICATION_CATEGORY_EMAIL;
  const canCreateTask = isEmailItem && !linkedTaskId && Boolean(onCreateTask);
  const canOpenTask = isEmailItem && Boolean(linkedTaskId) && Boolean(onOpenTask);

  return (
    <article
      role={isActionable ? "button" : undefined}
      tabIndex={isActionable ? 0 : undefined}
      onClick={
        isActionable
          ? () => {
              onMarkRead?.(item.id);
            }
          : undefined
      }
      onKeyDown={
        isActionable
          ? (event) => {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                onMarkRead?.(item.id);
              }
            }
          : undefined
      }
      className={`flex gap-3 rounded-xl border px-4 py-3 shadow-[inset_0_1px_0_rgba(255,255,255,0.05)] ${
        item.isRead
          ? "border-white/[0.08] bg-black/25"
          : "border-emerald-400/20 bg-emerald-500/[0.06] ring-1 ring-emerald-400/15"
      } ${isActionable ? "cursor-pointer transition hover:border-emerald-400/35" : ""}`.trim()}
    >
      <span
        className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-xl ring-1 ${CATEGORY_ICON_WRAP[item.category]}`}
      >
        {categoryIcon(item.category)}
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <h3 className="text-[15px] font-semibold leading-snug text-white">{item.title}</h3>
          {!item.isRead ? (
            <span className="shrink-0 rounded-full bg-emerald-400/90 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-zinc-950">
              新
            </span>
          ) : null}
        </div>
        <p className="mt-1 text-[13px] leading-relaxed text-white/65">{item.body}</p>
        <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-white/45">
          <span className="rounded-md bg-white/8 px-1.5 py-0.5 font-medium text-white/70">
            {NOTIFICATION_CATEGORY_LABEL[item.category]}
          </span>
          <span title={format(created, "PPpp", { locale: zhCN })}>{relative}</span>
          <span className="tabular-nums text-white/35">{format(created, "M月d日 HH:mm", { locale: zhCN })}</span>
        </div>
        {canOpenTask && linkedTaskId ? (
          <div className="mt-2">
            <button
              type="button"
              onClick={(event) => {
                event.stopPropagation();
                onOpenTask?.(linkedTaskId);
              }}
              className="rounded-lg border border-sky-400/30 bg-sky-500/15 px-3 py-1 text-[12px] font-medium text-sky-100 transition hover:bg-sky-500/25"
            >
              查看任务
            </button>
          </div>
        ) : null}
        {canCreateTask ? (
          <div className="mt-2">
            <button
              type="button"
              onClick={(event) => {
                event.stopPropagation();
                onCreateTask?.(item.id);
              }}
              className="rounded-lg border border-sky-400/30 bg-sky-500/15 px-3 py-1 text-[12px] font-medium text-sky-100 transition hover:bg-sky-500/25"
            >
              转成任务
            </button>
          </div>
        ) : null}
      </div>
    </article>
  );
}
