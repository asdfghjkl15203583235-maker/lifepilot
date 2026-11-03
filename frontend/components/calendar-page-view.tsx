"use client";

import {
  addMonths,
  eachDayOfInterval,
  endOfMonth,
  endOfWeek,
  format,
  isSameDay,
  isSameMonth,
  isToday,
  startOfMonth,
  startOfWeek,
  subMonths,
} from "date-fns";
import { useMemo, useState } from "react";

import { useRouter } from "next/navigation";

import {
  type LocalCalendarEvent,
  useCalendarEvents,
  useSyncLocalCalendar,
} from "@/hooks/use-calendar-events";
import type { Task } from "@/hooks/use-tasks";
import { useUpdateTaskMutation } from "@/hooks/use-tasks";

const WEEK_STARTS_ON_SUNDAY = 0;

const WEEKDAY_LABELS = ["日", "一", "二", "三", "四", "五", "六"] as const;

function getCalendarWeeks(visibleMonth: Date): Date[][] {
  const monthStart = startOfMonth(visibleMonth);
  const monthEnd = endOfMonth(visibleMonth);
  const gridStart = startOfWeek(monthStart, { weekStartsOn: WEEK_STARTS_ON_SUNDAY });
  const gridEnd = endOfWeek(monthEnd, { weekStartsOn: WEEK_STARTS_ON_SUNDAY });
  const days = eachDayOfInterval({ start: gridStart, end: gridEnd });
  const weeks: Date[][] = [];
  for (let index = 0; index < days.length; index += 7) {
    weeks.push(days.slice(index, index + 7));
  }
  return weeks;
}

function formatEventRange(event: LocalCalendarEvent): string {
  const dateLine = (d: Date) => `${d.getFullYear()}年${d.getMonth() + 1}月${d.getDate()}日`;
  const timeLine = (d: Date) =>
    `${d.getHours().toString().padStart(2, "0")}:${d.getMinutes().toString().padStart(2, "0")}`;
  const start = new Date(event.starts_at);
  const end = event.ends_at ? new Date(event.ends_at) : null;
  if (event.all_day) {
    return `${dateLine(start)} 全天`;
  }
  if (!end || isSameDay(start, end)) {
    return `${dateLine(start)} ${timeLine(start)}${end ? `–${timeLine(end)}` : ""}`;
  }
  return `${dateLine(start)} ${timeLine(start)} – ${dateLine(end)} ${timeLine(end)}`;
}

function formatDueLabel(dueAt: string | null): string {
  if (!dueAt) return "";
  const d = new Date(dueAt);
  if (Number.isNaN(d.getTime())) return "";
  const now = new Date();
  if (isSameDay(d, now)) return `今天 ${d.getHours().toString().padStart(2, "0")}:${d.getMinutes().toString().padStart(2, "0")}`;
  return `${d.getMonth() + 1}月${d.getDate()}日 ${d.getHours().toString().padStart(2, "0")}:${d.getMinutes().toString().padStart(2, "0")}`;
}

function IconChevronLeft({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} aria-hidden>
      <path strokeLinecap="round" strokeLinejoin="round" d="M15 19l-7-7 7-7" />
    </svg>
  );
}

function IconChevronRight({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} aria-hidden>
      <path strokeLinecap="round" strokeLinejoin="round" d="M9 5l7 7-7 7" />
    </svg>
  );
}

function IconClock({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.75} aria-hidden>
      <path strokeLinecap="round" strokeLinejoin="round" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
    </svg>
  );
}

function IconTask({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.75} aria-hidden>
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-6 9l2 2 4-4"
      />
    </svg>
  );
}

function IconSearch({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} aria-hidden>
      <path strokeLinecap="round" strokeLinejoin="round" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
    </svg>
  );
}

function IconRefresh({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} aria-hidden>
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"
      />
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

function taskOnDay(task: Task, day: Date): boolean {
  if (!task.due_at) return false;
  return isSameDay(new Date(task.due_at), day);
}

function eventOnDay(event: LocalCalendarEvent, day: Date): boolean {
  return isSameDay(new Date(event.starts_at), day);
}

function statusPillClasses(status: Task["status"]): string {
  switch (status) {
    case "done":
      return "bg-emerald-500/15 text-emerald-200/95 ring-emerald-400/25";
    case "in_progress":
      return "bg-amber-500/15 text-amber-200/95 ring-amber-400/25";
    default:
      return "bg-white/8 text-white/78 ring-white/12";
  }
}

function statusLabel(status: Task["status"]): string {
  switch (status) {
    case "done":
      return "已完成";
    case "in_progress":
      return "进行中";
    default:
      return "待办";
  }
}

export function CalendarPageView() {
  const router = useRouter();
  const [visibleMonth, setVisibleMonth] = useState(() => startOfMonth(new Date()));
  const [selectedDay, setSelectedDay] = useState(() => new Date());
  const [eventSearch, setEventSearch] = useState("");
  const [highlightedId, setHighlightedId] = useState<string | null>(null);

  const monthStart = startOfMonth(visibleMonth);
  const monthEnd = endOfMonth(visibleMonth);
  const rangeEnd = endOfWeek(monthEnd, { weekStartsOn: WEEK_STARTS_ON_SUNDAY });

  const { data, isPending, isError, refetch } = useCalendarEvents([monthStart, rangeEnd] as const);
  const updateTask = useUpdateTaskMutation();
  const syncLocal = useSyncLocalCalendar();

  const events = useMemo(() => data?.events ?? [], [data]);
  const tasks = useMemo(() => data?.tasks ?? [], [data]);

  const dayHasMarker = useMemo(() => {
    const set = new Set<string>();
    for (const event of events) set.add(format(new Date(event.starts_at), "yyyy-MM-dd"));
    for (const task of tasks) {
      if (task.due_at) set.add(format(new Date(task.due_at), "yyyy-MM-dd"));
    }
    return set;
  }, [events, tasks]);

  const weeks = useMemo(() => getCalendarWeeks(visibleMonth), [visibleMonth]);

  const eventsForSelectedDay = useMemo(
    () => events.filter((event) => eventOnDay(event, selectedDay)),
    [events, selectedDay],
  );
  const tasksForSelectedDay = useMemo(
    () => tasks.filter((task) => taskOnDay(task, selectedDay)),
    [tasks, selectedDay],
  );

  const filteredEvents = useMemo(() => {
    const query = eventSearch.trim().toLowerCase();
    if (!query) return eventsForSelectedDay;
    return eventsForSelectedDay.filter((event) => event.title.toLowerCase().includes(query));
  }, [eventsForSelectedDay, eventSearch]);

  const syncResultText = syncLocal.isSuccess
    ? `同步完成：新增 ${syncLocal.data.imported}，更新 ${syncLocal.data.updated}`
    : null;

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-5 overflow-y-auto overscroll-contain px-1 pb-4 pt-2 sm:px-2">
      <section
        aria-labelledby="custom-calendar-heading"
        className="rounded-2xl border border-white/12 bg-black/50 p-4 shadow-[inset_0_1px_0_rgba(255,255,255,0.06)] backdrop-blur-md sm:p-5"
      >
        <h2 id="custom-calendar-heading" className="sr-only">
          月视图
        </h2>

        <div className="mb-5 flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => setVisibleMonth((previous) => subMonths(previous, 1))}
              className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl border border-white/15 bg-white/6 text-white/75 transition hover:border-white/25 hover:bg-white/12 hover:text-white"
              aria-label="上个月"
            >
              <IconChevronLeft className="h-5 w-5" />
            </button>
            <p className="min-w-0 text-lg font-semibold tracking-tight text-white/95">
              {visibleMonth.getFullYear()}年{visibleMonth.getMonth() + 1}月
            </p>
            <button
              type="button"
              onClick={() => setVisibleMonth((previous) => addMonths(previous, 1))}
              className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl border border-white/15 bg-white/6 text-white/75 transition hover:border-white/25 hover:bg-white/12 hover:text-white"
              aria-label="下个月"
            >
              <IconChevronRight className="h-5 w-5" />
            </button>
          </div>
          <div className="flex items-center gap-2">
            {syncResultText ? <p className="text-[12px] text-emerald-200/80">{syncResultText}</p> : null}
            <button
              type="button"
              onClick={() => syncLocal.mutate()}
              disabled={syncLocal.isPending}
              className="inline-flex items-center gap-1.5 rounded-full border border-white/15 bg-white/8 px-3 py-1.5 text-[12px] font-medium text-white/85 transition hover:bg-white/14 disabled:opacity-50"
            >
              <IconRefresh className="h-3.5 w-3.5" />
              {syncLocal.isPending ? "同步中…" : "同步本地日历"}
            </button>
          </div>
        </div>

        <div className="mb-2 grid grid-cols-7 gap-2 text-center">
          {WEEKDAY_LABELS.map((label) => (
            <div key={label} className="py-2 text-[11px] font-medium uppercase tracking-[0.12em] text-white/38">
              {label}
            </div>
          ))}
        </div>

        <div className="grid gap-2">
          {weeks.map((week) => (
            <div key={week[0].toISOString()} className="grid grid-cols-7 gap-2">
              {week.map((day) => {
                const inMonth = isSameMonth(day, visibleMonth);
                const selected = isSameDay(day, selectedDay);
                const today = isToday(day);
                const dayKey = format(day, "yyyy-MM-dd");
                const hasMarker = dayHasMarker.has(dayKey);

                return (
                  <button
                    key={day.toISOString()}
                    type="button"
                    onClick={() => {
                      setSelectedDay(day);
                      if (!isSameMonth(day, visibleMonth)) {
                        setVisibleMonth(startOfMonth(day));
                      }
                    }}
                    className={`relative flex min-h-13 flex-col items-center justify-center rounded-2xl text-sm font-medium transition ${
                      selected
                        ? "bg-linear-to-b from-orange-500 via-rose-500 to-rose-600 text-white shadow-[0_10px_28px_-12px_rgba(249,115,22,0.55)]"
                        : today
                          ? "ring-2 ring-rose-400/75 ring-offset-2 ring-offset-zinc-950 bg-white/6 text-white/90 hover:bg-white/10"
                          : "bg-white/6 text-white/88 hover:bg-white/11"
                    } ${!inMonth && !selected ? "text-white/30" : ""} `.trim()}
                  >
                    <span className={`relative z-1 ${!inMonth && !selected ? "opacity-55" : ""}`}>
                      {format(day, "d")}
                    </span>
                    {hasMarker ? (
                      <span
                        className={`absolute bottom-2 left-1/2 h-[5px] w-5 -translate-x-1/2 rounded-full ${
                          selected ? "bg-white/90" : "bg-fuchsia-400/90"
                        }`}
                        aria-hidden
                      />
                    ) : null}
                  </button>
                );
              })}
            </div>
          ))}
        </div>
      </section>

      <section className="rounded-2xl border border-white/12 bg-black/50 p-4 shadow-[inset_0_1px_0_rgba(255,255,255,0.06)] backdrop-blur-md sm:p-5">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="text-xs text-white/40">
            {isPending
              ? "加载中…"
              : isError
                ? "加载失败，请重试"
                : `${filteredEvents.length} 个日程 · ${tasksForSelectedDay.length} 个任务${
                    eventsForSelectedDay.length + tasksForSelectedDay.length > 0
                      ? ` · ${selectedDay.getMonth() + 1}月${selectedDay.getDate()}日`
                      : ""
                  }`}
          </p>
          <button
            type="button"
            onClick={() => void refetch()}
            className="inline-flex items-center gap-1.5 rounded-full border border-white/12 bg-white/6 px-2.5 py-1 text-[11px] font-medium text-white/60 transition hover:bg-white/12 hover:text-white/90"
          >
            <IconRefresh className="h-3 w-3" />
            刷新
          </button>
        </div>

        <ul className="mt-4 space-y-1">
          {filteredEvents.length === 0 && tasksForSelectedDay.length === 0 ? (
            <li className="rounded-xl border border-white/8 bg-white/3 px-4 py-8 text-center text-sm text-white/45">
              当天没有日程或任务。
            </li>
          ) : (
            <>
              {filteredEvents.map((event) => {
                const isHighlighted = highlightedId === event.id;
                return (
                  <li key={event.id}>
                    <button
                      type="button"
                      onClick={() => setHighlightedId(event.id)}
                      className={`flex w-full flex-col gap-2 rounded-xl border px-4 py-3 text-left transition ${
                        isHighlighted
                          ? "border-white/18 bg-white/9"
                          : "border-transparent bg-transparent hover:border-white/10 hover:bg-white/5"
                      }`}
                    >
                      <div className="flex items-start justify-between gap-3">
                        <span className="inline-flex min-w-0 items-center gap-2 font-semibold leading-snug text-white/92">
                          <IconClock className="h-4 w-4 shrink-0 text-fuchsia-300/70" />
                          <span className="truncate">{event.title}</span>
                        </span>
                        {event.all_day ? (
                          <span className="shrink-0 rounded-md bg-white/8 px-2 py-0.5 text-[11px] text-white/55">全天</span>
                        ) : null}
                      </div>
                      <div className="flex items-center gap-2 text-[13px] text-white/48">
                        <span>{formatEventRange(event)}</span>
                        {event.location ? (
                          <span className="truncate text-white/35">· {event.location}</span>
                        ) : null}
                      </div>
                    </button>
                  </li>
                );
              })}
              {tasksForSelectedDay.map((task) => {
                const isDone = task.status === "done";
                const isHighlighted = highlightedId === task.id;
                return (
                  <li key={task.id}>
                    <div
                      className={`flex w-full items-start gap-2 rounded-xl border px-4 py-3 text-left transition ${
                        isHighlighted
                          ? "border-white/18 bg-white/9"
                          : "border-transparent bg-transparent hover:border-white/10 hover:bg-white/5"
                      }`}
                    >
                      <button
                        type="button"
                        aria-label={isDone ? "标记为未完成" : "标记为已完成"}
                        onClick={() => updateTask.mutate({ id: task.id, status: isDone ? "not_started" : "done" })}
                        className={`mt-0.5 flex h-4 w-4 shrink-0 cursor-pointer items-center justify-center rounded border transition ${
                          isDone
                            ? "border-emerald-400/50 bg-emerald-400/20 text-emerald-200"
                            : "border-white/30 bg-transparent text-transparent hover:border-emerald-400/60 hover:text-emerald-300/50"
                        }`}
                      >
                        <IconCheck className="h-3 w-3" />
                      </button>
                      <button
                        type="button"
                        onClick={() => {
                          setHighlightedId(task.id);
                          router.push(`/tasks?id=${task.id}`);
                        }}
                        className="min-w-0 flex-1 text-left"
                      >
                        <div className="flex items-start justify-between gap-3">
                          <span className="inline-flex min-w-0 items-center gap-2 font-semibold leading-snug text-white/92">
                            <IconTask className="h-4 w-4 shrink-0 text-sky-300/70" />
                            <span className={`truncate ${isDone ? "text-white/45 line-through" : ""}`}>{task.title}</span>
                          </span>
                          <span
                            className={`shrink-0 rounded-md px-2 py-0.5 text-[11px] font-medium ring-1 ${statusPillClasses(task.status)}`}
                          >
                            {statusLabel(task.status)}
                          </span>
                        </div>
                        <div className="mt-1 flex items-center gap-2 text-[13px] text-white/48">
                          <span>截止 {formatDueLabel(task.due_at)}</span>
                        </div>
                      </button>
                    </div>
                  </li>
                );
              })}
            </>
          )}
        </ul>

        <div className="relative mt-5">
          <IconSearch className="pointer-events-none absolute left-3.5 top-1/2 h-4 w-4 -translate-y-1/2 text-white/35" />
          <input
            type="search"
            value={eventSearch}
            onChange={(event) => setEventSearch(event.target.value)}
            placeholder="搜索日程..."
            className="w-full rounded-xl border border-white/12 bg-black/35 py-3 pl-10 pr-4 text-sm text-white/90 shadow-inner placeholder:text-white/35 focus:border-violet-400/40 focus:outline-none focus:ring-2 focus:ring-violet-500/30"
            aria-label="搜索日程"
          />
        </div>
      </section>
    </div>
  );
}