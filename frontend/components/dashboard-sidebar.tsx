"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import { useNextUpcoming } from "@/hooks/use-calendar-events";
import {
  useDeleteTaskMutation,
  useTaskScanPlanQuery,
  useTasksQuery,
  useUpdateTaskMutation,
} from "@/hooks/use-tasks";

import { QuickCreateTaskDialog } from "./quick-create-task-dialog";

const STATUS_STYLE: Record<string, { label: string; className: string }> = {
  not_started: { label: "未开始", className: "bg-zinc-500/15 text-zinc-300" },
  in_progress: { label: "进行中", className: "bg-amber-500/15 text-amber-200" },
  done: { label: "已完成", className: "bg-emerald-500/15 text-emerald-200" },
};

function IconGiftTag({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" aria-hidden>
      <path
        fill="#dc2626"
        d="M4 9a2 2 0 012-2h2.5a2 2 0 011.6.8L12 11l2.9-3.2A2 2 0 0116.5 7H19a2 2 0 012 2v2.5a2 2 0 01-.6 1.4l-7 8.2a2 2 0 01-3.08 0l-7-8.2A2 2 0 014 11.5V9z"
      />
      <path fill="#fca5a5" d="M8 5h2v4H8z" />
    </svg>
  );
}

function IconDotsHorizontal({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="currentColor" aria-hidden>
      <circle cx="6" cy="12" r="2" />
      <circle cx="12" cy="12" r="2" />
      <circle cx="18" cy="12" r="2" />
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

function IconClockSmall({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.75} aria-hidden>
      <path strokeLinecap="round" strokeLinejoin="round" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
    </svg>
  );
}

function IconCheck({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} aria-hidden>
      <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
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

const STATUS_DOT: Record<string, { color: string; label: string }> = {
  not_started: { color: "bg-zinc-400", label: "未开始" },
  in_progress: { color: "bg-amber-400", label: "进行中" },
  done: { color: "bg-emerald-400", label: "已完成" },
};

/** 权威状态：只要 due_at 为空或非法，绝不允许显示"已逾期/临近截止"，统一显示"待办"。 */
function hasValidDueTime(dueAt: string | null): boolean {
  if (!dueAt) return false;
  const due = new Date(dueAt);
  return !Number.isNaN(due.getTime());
}

/** 按「距离截止日期远近」排序：最近的在前，无截止日期的排最后。 */
function compareByDueDate(aDueAt: string | null, bDueAt: string | null): number {
  const aDue = hasValidDueTime(aDueAt) ? new Date(aDueAt!).getTime() : Number.POSITIVE_INFINITY;
  const bDue = hasValidDueTime(bDueAt) ? new Date(bDueAt!).getTime() : Number.POSITIVE_INFINITY;
  if (aDue === bDue) return 0;
  return aDue - bDue;
}

function resolveActivityAction(dueAt: string | null, reason: string): string {
  return hasValidDueTime(dueAt) ? reason : "待办";
}

function formatUpcomingDate(date: Date): string {
  const today = new Date();
  const sameDay =
    date.getFullYear() === today.getFullYear() &&
    date.getMonth() === today.getMonth() &&
    date.getDate() === today.getDate();
  const weekdays = ["星期日", "星期一", "星期二", "星期三", "星期四", "星期五", "星期六"];
  if (sameDay) return `今天 · ${weekdays[date.getDay()]}`;
  return `${date.getMonth() + 1}月${date.getDate()}日 · ${weekdays[date.getDay()]}`;
}

function formatUpcomingTime(date: Date): string {
  const hours = date.getHours().toString().padStart(2, "0");
  const minutes = date.getMinutes().toString().padStart(2, "0");
  return `${hours}:${minutes}`;
}

function CountdownText({ dueAt }: { dueAt: string | null }) {
  // 无截止时间（或时间非法/已过）→ 统一灰色"待办"标签，绝不显示"已逾期/临近截止"。
  if (!dueAt) return <span className="text-white/35">待办</span>;
  const due = new Date(dueAt);
  if (Number.isNaN(due.getTime())) return <span className="text-white/35">待办</span>;
  const diffMs = due.getTime() - Date.now();
  if (diffMs <= 0) return <span className="text-white/35">待办</span>;
  const mins = Math.round(diffMs / 60_000);
  if (mins < 1) return <span className="text-white/35">待办</span>;
  if (mins < 60) return <>⏰ {mins} 分钟后</>;
  const hours = Math.floor(mins / 60);
  if (hours < 24) {
    const rem = mins % 60;
    return rem ? <>⏰ {hours} 小时{rem} 分后</> : <>⏰ {hours} 小时后</>;
  }
  const days = Math.floor(hours / 24);
  return <>⏰ {days} 天后</>;
}

export interface DashboardSidebarProps {
  isAgentTracesOpen: boolean;
  onAgentTracesToggle: () => void;
}

export function DashboardSidebar({ isAgentTracesOpen, onAgentTracesToggle }: DashboardSidebarProps) {
  const router = useRouter();
  const { data: tasks = [], isPending: isTasksPending } = useTasksQuery();
  const {
    data: scanPlan,
    isPending: isScanPending,
    isError: isScanError,
    refetch: refetchScan,
  } = useTaskScanPlanQuery();
  const updateTask = useUpdateTaskMutation();
  const deleteTask = useDeleteTaskMutation();
  const { next: nextUpcoming } = useNextUpcoming();
  const [isCardMenuOpen, setIsCardMenuOpen] = useState(false);
  const [isQuickCreateOpen, setIsQuickCreateOpen] = useState(false);
  const cardMenuRef = useRef<HTMLDivElement | null>(null);

  // 点击卡片外部 / 按 Esc 收起：菜单绝对定位在卡片右上角，不监听就会一直开着。
  useEffect(() => {
    if (!isCardMenuOpen) return;
    const handlePointerDown = (event: MouseEvent) => {
      if (!(event.target instanceof Element)) return;
      if (!cardMenuRef.current?.contains(event.target)) {
        setIsCardMenuOpen(false);
      }
    };
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setIsCardMenuOpen(false);
    };
    document.addEventListener("mousedown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("mousedown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [isCardMenuOpen]);

  const userTasks = tasks.filter((task) => task.source !== "system");

  const summary = scanPlan?.summary;
  const stats = summary
    ? [
        { label: "总任务", value: String(summary.total), hint: "全局" },
        { label: "待执行", value: String(summary.ready), hint: "稍后可做" },
        { label: "进行中", value: String(summary.in_progress), hint: "正在执行" },
        { label: "需处理", value: String(summary.overdue + summary.blocked), hint: "逾期/受阻" },
      ]
    : [
        { label: "总任务", value: userTasks.length ? String(userTasks.length) : "…", hint: isTasksPending ? "加载中" : "全局" },
        { label: "待执行", value: "…", hint: "稍后" },
        { label: "进行中", value: "…", hint: "正在执行" },
        { label: "需处理", value: "…", hint: "逾期/受阻" },
      ];

  const prioritized = [
    ...(scanPlan?.overdue.map((item) => ({ item, reason: "已逾期" })) ?? []),
    ...(scanPlan?.blocked.map((item) => ({ item, reason: "依赖阻塞" })) ?? []),
    ...(scanPlan?.upcoming.map((item) => ({ item, reason: "临近截止" })) ?? []),
    ...(scanPlan?.ready.map((item) => ({ item, reason: "已就绪" })) ?? []),
  ]
    .filter(({ item }) => item.source !== "system")
    // 已完成的条目绝不能再出现在"待办"里：否则勾选完成后它会留在原位，
    // 用户以为没生效。
    .filter(({ item }) => item.status !== "done")
    // 按截止日期远近排序（最近在前，无截止日期最后），而不是按优先级混排。
    .sort((a, b) => compareByDueDate(a.item.due_at, b.item.due_at));

  const visibleActivities =
    prioritized.length > 0
      ? prioritized.map(({ item, reason }) => ({
          key: `${item.id}-${reason}`,
          subject: item.title,
          action: resolveActivityAction(item.due_at, reason),
          status: item.status,
          dueAt: item.due_at,
          id: item.id,
          isDone: item.status === "done",
          hasDue: hasValidDueTime(item.due_at),
        }))
      : userTasks
          .filter((task) => task.status !== "done")
          .sort((a, b) => compareByDueDate(a.due_at, b.due_at))
          .map((task) => ({
            key: task.id,
            subject: task.title,
            action: resolveActivityAction(task.due_at, STATUS_STYLE[task.status]?.label ?? task.status),
            status: task.status,
            dueAt: task.due_at,
            id: task.id,
            isDone: task.status === "done",
            hasDue: hasValidDueTime(task.due_at),
          }));

  const markDone = (taskId: string) => {
    updateTask.mutate({ id: taskId, status: "done" });
  };

  return (
    <aside
      className="flex h-full min-h-0 w-full shrink-0 flex-col gap-4 overflow-hidden border border-white/12 bg-black/55 p-4 shadow-[inset_0_1px_0_rgba(255,255,255,0.06)] backdrop-blur-md lg:w-72 lg:self-stretch"
      aria-label="仪表盘 — 统计、日历和代理活动"
    >
      <div className="shrink-0">
        <p className="text-[11px] font-semibold uppercase tracking-[0.14em] text-white/40">上下文</p>
        <p className="mt-0.5 text-sm font-medium text-white/85">今天 · 概览</p>
      </div>

      <section className="shrink-0 rounded-xl border border-white/10 bg-black/35 p-3">
        <h2 className="mb-3 text-[11px] font-semibold uppercase tracking-wide text-white/45">快速统计</h2>
        <ul className="grid grid-cols-2 gap-2">
          {stats.map((stat) => (
            <li key={stat.label} className="rounded-lg border border-white/5 bg-white/4 px-3 py-2">
              <p className="text-[11px] leading-tight text-white/45">{stat.label}</p>
              <p className="mt-0.5 text-lg font-semibold tabular-nums leading-none text-white">{stat.value}</p>
              <p className="mt-1 text-[10px] leading-tight text-white/35">{stat.hint}</p>
            </li>
          ))}
        </ul>
      </section>

      <section className="shrink-0" aria-labelledby="sidebar-calendar-heading">
        <h2 id="sidebar-calendar-heading" className="sr-only">
          下一项日程
        </h2>
        <div className="rounded-2xl border border-white/12 bg-black/35 px-4 pb-4 pt-3 shadow-[inset_0_1px_0_rgba(255,255,255,0.06)] backdrop-blur-sm">
          <div className="flex items-start justify-between gap-2">
            <span className="flex h-8 w-8 shrink-0 items-center justify-center" aria-hidden>
              <IconGiftTag className="h-7 w-7 drop-shadow-[0_2px_8px_rgba(0,0,0,0.35)]" />
            </span>
            <div ref={cardMenuRef} className="relative">
              <button
                type="button"
                onClick={() => setIsCardMenuOpen((open) => !open)}
                aria-label="日程选项"
                aria-haspopup="menu"
                aria-expanded={isCardMenuOpen}
                className="rounded-md p-1 text-white/45 transition hover:bg-white/10 hover:text-white/90"
              >
                <IconDotsHorizontal className="h-5 w-5" />
              </button>
              {isCardMenuOpen ? (
                // z-50：卡片外层是 overflow-hidden 的侧栏，层级太低会被任务列表盖住。
                <div
                  role="menu"
                  className="absolute right-0 top-8 z-50 w-36 overflow-hidden rounded-lg border border-white/15 bg-zinc-900/95 py-1 shadow-xl backdrop-blur-md"
                >
                  {nextUpcoming?.kind === "task" ? (
                    <>
                      <button
                        type="button"
                        role="menuitem"
                        onClick={() => {
                          setIsCardMenuOpen(false);
                          router.push(`/tasks?id=${nextUpcoming.id}`);
                        }}
                        className="flex w-full items-center gap-2 px-3 py-2 text-left text-[12px] font-medium text-white/80 transition hover:bg-white/10 hover:text-white"
                      >
                        查看任务
                      </button>
                      <button
                        type="button"
                        role="menuitem"
                        onClick={() => {
                          setIsCardMenuOpen(false);
                          markDone(nextUpcoming.id);
                        }}
                        className="flex w-full items-center gap-2 px-3 py-2 text-left text-[12px] font-medium text-white/80 transition hover:bg-white/10 hover:text-white"
                      >
                        标为完成
                      </button>
                      <button
                        type="button"
                        role="menuitem"
                        onClick={() => {
                          setIsCardMenuOpen(false);
                          if (!window.confirm(`确定删除「${nextUpcoming.title}」吗？删除后无法恢复。`)) {
                            return;
                          }
                          deleteTask.mutate(nextUpcoming.id);
                        }}
                        className="flex w-full items-center gap-2 px-3 py-2 text-left text-[12px] font-medium text-rose-300 transition hover:bg-rose-500/15 hover:text-rose-100"
                      >
                        删除任务
                      </button>
                    </>
                  ) : (
                    <button
                      type="button"
                      role="menuitem"
                      onClick={() => {
                        setIsCardMenuOpen(false);
                        router.push("/calendar");
                      }}
                      className="flex w-full items-center gap-2 px-3 py-2 text-left text-[12px] font-medium text-white/80 transition hover:bg-white/10 hover:text-white"
                    >
                      查看全部日历
                    </button>
                  )}
                </div>
              ) : null}
            </div>
          </div>

          {nextUpcoming ? (
            <>
              <h3 className="mt-1 break-words text-xl font-bold leading-snug tracking-tight text-white/95">
                {nextUpcoming.title}
              </h3>
              <div className="mt-3 space-y-2 text-sm font-medium text-white/78">
                <div className="flex items-center gap-2">
                  {nextUpcoming.kind === "event" ? (
                    <IconCalendarSmall className="h-4 w-4 shrink-0 text-white/45" />
                  ) : (
                    <IconClockSmall className="h-4 w-4 shrink-0 text-white/45" />
                  )}
                  <span>{formatUpcomingDate(nextUpcoming.startsAt)}</span>
                </div>
                <div className="flex items-center gap-2">
                  <IconClockSmall className="h-4 w-4 shrink-0 text-white/45" />
                  <span>{formatUpcomingTime(nextUpcoming.startsAt)}</span>
                </div>
              </div>
            </>
          ) : (
            <h3 className="mt-1 text-xl font-bold leading-snug tracking-tight text-white/95">
              暂无日程
            </h3>
          )}

          <div className="my-4 h-px bg-white/10" />

          <div className="flex items-center gap-2">
            {nextUpcoming?.kind === "task" ? (
              <Link
                href={`/tasks?id=${nextUpcoming.id}`}
                className="flex flex-1 items-center justify-center gap-2 rounded-full border border-white/20 bg-white/8 px-4 py-2.5 text-sm font-medium text-white/92 shadow-sm transition hover:border-white/28 hover:bg-white/14"
              >
                <IconBellSmall className="h-4 w-4 text-white/75" aria-hidden />
                查看任务
              </Link>
            ) : null}
            <Link
              href="/calendar"
              className="flex flex-1 items-center justify-center gap-2 rounded-full border border-white/20 bg-white/8 px-4 py-2.5 text-sm font-medium text-white/92 shadow-sm transition hover:border-white/28 hover:bg-white/14"
            >
              <IconCalendarSmall className="h-4 w-4 text-white/75" aria-hidden />
              查看全部日历
            </Link>
          </div>
        </div>
      </section>

      <section className="flex min-h-0 flex-1 flex-col rounded-xl border border-white/10 bg-black/35 p-3">
        <div className="mb-2 flex shrink-0 items-center justify-between gap-2">
          <h2 className="text-[11px] font-semibold uppercase tracking-wide text-white/45">任务概览</h2>
          <div className="flex shrink-0 items-center gap-1.5">
            <button
              type="button"
              onClick={() => setIsQuickCreateOpen(true)}
              aria-label="新建任务"
              title="新建任务"
              className="flex h-5 w-5 shrink-0 items-center justify-center rounded-md border border-white/15 bg-white/10 text-[13px] font-semibold leading-none text-white/80 transition hover:bg-white/20 hover:text-white"
            >
              +
            </button>
            <button
              type="button"
              onClick={() => void refetchScan()}
              className="shrink-0 text-[11px] font-medium text-white/45 transition hover:text-white"
            >
              {isScanPending ? "扫描中…" : "刷新"}
            </button>
            <button
              type="button"
              onClick={onAgentTracesToggle}
              aria-pressed={isAgentTracesOpen}
              aria-label={isAgentTracesOpen ? "关闭执行详情" : "查看执行详情"}
              title="执行详情"
              className={`flex h-5 w-5 shrink-0 items-center justify-center rounded-md text-[12px] leading-none transition ${
                isAgentTracesOpen
                  ? "bg-violet-500/30 text-white ring-1 ring-violet-300/40"
                  : "text-white/45 hover:bg-white/10 hover:text-white"
              }`}
            >
              <span aria-hidden>ℹ</span>
            </button>
          </div>
        </div>

        {isScanError && prioritized.length === 0 ? (
          <p className="shrink-0 py-2 text-[11px] text-rose-300">
            扫描失败，点击刷新重试。{" "}
            <button type="button" onClick={() => void refetchScan()} className="underline">
              重试
            </button>
          </p>
        ) : null}

        {visibleActivities.length === 0 ? (
          <p className="shrink-0 py-2 text-[11px] text-white/35">暂无待办任务。</p>
        ) : (
          <ul className="sidebar-task-scroll flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto overscroll-contain pb-4 pr-1">
            {visibleActivities.map((activity) => {
              const dot = STATUS_DOT[activity.status] ?? STATUS_DOT.not_started;
              return (
                <li
                  key={activity.key}
                  className="group min-h-0 shrink-0 cursor-pointer rounded-lg border border-white/5 bg-white/4 px-2.5 py-2 transition hover:border-white/12 hover:bg-white/7"
                  onClick={() => router.push(`/tasks?id=${activity.id}`)}
                >
                  <div className="flex items-start gap-2">
                    <span
                      role="checkbox"
                      aria-checked={activity.isDone}
                      aria-label="标为已完成"
                      tabIndex={0}
                      onClick={(event) => {
                        event.stopPropagation();
                        if (!activity.isDone) markDone(activity.id);
                      }}
                      onKeyDown={(event) => {
                        if (event.key === "Enter" || event.key === " ") {
                          event.preventDefault();
                          event.stopPropagation();
                          if (!activity.isDone) markDone(activity.id);
                        }
                      }}
                      className={`mt-0.5 flex h-4 w-4 shrink-0 cursor-pointer items-center justify-center rounded border transition ${
                        activity.isDone
                          ? "border-emerald-400/50 bg-emerald-400/20 text-emerald-200"
                          : "border-white/30 bg-transparent text-transparent hover:border-emerald-400/60 hover:text-emerald-300/50"
                      }`}
                    >
                      <IconCheck className="h-3 w-3" />
                    </span>
                    <div className="min-w-0 flex-1">
                      <p className={`line-clamp-2 text-sm leading-snug ${activity.isDone ? "text-white/45 line-through" : "text-white/88"}`}>
                        {activity.subject}
                      </p>
                      <p className="mt-0.5 text-[11px] text-amber-200/70">
                        {activity.hasDue ? <CountdownText dueAt={activity.dueAt} /> : "待办"}
                      </p>
                    </div>
                  </div>
                  {activity.hasDue ? (
                    <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[11px] text-white/45">
                      <span className="inline-flex items-center gap-1">
                        <span className={`h-1.5 w-1.5 rounded-full ${dot.color} ${activity.isDone ? "opacity-40" : ""}`} />
                        <span className={activity.isDone ? "text-white/40" : ""}>{activity.action}</span>
                      </span>
                    </div>
                  ) : null}
                </li>
              );
            })}
          </ul>
        )}
      </section>

      {isQuickCreateOpen ? (
        <QuickCreateTaskDialog onClose={() => setIsQuickCreateOpen(false)} />
      ) : null}
    </aside>
  );
}
