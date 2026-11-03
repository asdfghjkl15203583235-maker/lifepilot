"use client";

import Link from "next/link";
import { useMemo, useState, type ReactNode } from "react";

import { DailyBriefingCard } from "@/components/daily-briefing-card";
import {
  useLifePilotBundle,
  useLifeServiceMutation,
  useMemoryCreate,
  useMemoryDelete,
} from "@/hooks/use-life-pilot";

function IconWeather({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.75} aria-hidden>
      <path strokeLinecap="round" strokeLinejoin="round" d="M8 13h.01M16 13h.01M12 9h.01M12 17h.01M3 15h18M5 15a2 2 0 01-2-2 3 3 0 015.4-1.8M19 15a2 2 0 001-3.7 3 3 0 00-5.4 1.8M8 15a4 4 0 017.6-2.1" />
    </svg>
  );
}

function IconMap({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.75} aria-hidden>
      <path strokeLinecap="round" strokeLinejoin="round" d="M9 20l-6 2V6l6-2 6 2 6-2v16l-6 2-6-2zM9 20V6m6 2v14" />
    </svg>
  );
}

function IconCalendar({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.75} aria-hidden>
      <path strokeLinecap="round" strokeLinejoin="round" d="M8 7V3m8 4V3m-9 8h10M5 21h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z" />
    </svg>
  );
}

function IconBell({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.75} aria-hidden>
      <path strokeLinecap="round" strokeLinejoin="round" d="M15 17h5l-1.405-1.405A2.032 2.032 0 0118 14.158V11a6.002 6.002 0 00-4-5.659V5a2 2 0 10-4 0v.341C7.67 6.165 6 8.388 6 11v3.159c0 .538-.214 1.055-.595 1.436L4 17h5m6 0v1a3 3 0 11-6 0v-1m6 0H9" />
    </svg>
  );
}

function formatRelativeDate(iso: string): string {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  return `${date.getMonth() + 1}月${date.getDate()}日 ${date.getHours().toString().padStart(2, "0")}:${date
    .getMinutes()
    .toString()
    .padStart(2, "0")}`;
}

function SectionShell({
  title,
  action,
  children,
}: {
  title: string;
  action?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="rounded-xl border border-white/12 bg-black/45 p-4 shadow-[inset_0_1px_0_rgba(255,255,255,0.06)] backdrop-blur-md sm:p-5">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-sm font-semibold text-white/95">{title}</h2>
        {action}
      </div>
      {children}
    </section>
  );
}

function StarterButton({
  icon,
  label,
  prompt,
}: {
  icon: ReactNode;
  label: string;
  prompt: string;
}) {
  return (
    <Link
      href={`/?q=${encodeURIComponent(prompt)}`}
      className="inline-flex items-center gap-2 rounded-full border border-white/20 bg-black/25 px-4 py-2.5 text-sm text-white/95 backdrop-blur-md transition hover:bg-black/35"
    >
      {icon}
      {label}
    </Link>
  );
}

function IconBookmark({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.75} aria-hidden>
      <path strokeLinecap="round" strokeLinejoin="round" d="M6 3h12a2 2 0 012 2v16l-8-4-8 4V5a2 2 0 012-2z" />
    </svg>
  );
}

function IconShield({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.75} aria-hidden>
      <path strokeLinecap="round" strokeLinejoin="round" d="M12 2l8 4v6c0 5-3.5 8.5-8 10-4.5-1.5-8-5-8-10V6l8-4z" />
    </svg>
  );
}

const MEMORY_TYPE_LABEL: Record<string, string> = {
  fact: "事实",
  rule: "规则",
  preference: "偏好",
  identity: "身份",
  goal: "目标",
  work: "工作",
  episodic: "经历",
};

/** 固定筛选类别全集（与后端 MemoryCategory 枚举一一对应）。
 *  实际渲染的 chips 由下面 useMemo 按"数据里真实存在的类别"动态派生，
 *  保证任何 chip 点下去都有内容，不会出现空筛。 */
const MEMORY_FILTER_ALL = "all" as const;
const MEMORY_FILTER_CHIP_ORDER = [
  "fact",
  "rule",
  "preference",
  "identity",
  "goal",
  "work",
  "episodic",
] as const;

type MemoryTypeValue = typeof MEMORY_FILTER_ALL | (typeof MEMORY_FILTER_CHIP_ORDER)[number];

export function LifePilotDashboard() {
  const { data, isPending, isError, error, refetch } = useLifePilotBundle();
  const [memoryDraft, setMemoryDraft] = useState("");
  const [memoryType, setMemoryType] = useState<MemoryTypeValue>("all");
  const [saveCategory, setSaveCategory] = useState<"fact" | "rule" | "preference">("preference");
  const [weatherCity, setWeatherCity] = useState("");
  const [mapQuery, setMapQuery] = useState("");

  const weatherMutation = useLifeServiceMutation("weather");
  const mapMutation = useLifeServiceMutation("map");
  const createMemory = useMemoryCreate();
  const deleteMemory = useMemoryDelete();

  const reminders = data?.reminders ?? [];
  const services = data?.services ?? [];
  const memories = data?.items ?? [];

  // 动态候选 chips：只展示真实出现的类别，"全部"恒在。这样点击任何 chip
  // 都有对应数据，不会出现"偏好"空筛等困惑。
  const memoryFilterChips = useMemo(() => {
    const present = new Set(memories.map((m) => m.category));
    return (["all", ...MEMORY_FILTER_CHIP_ORDER] as const).filter(
      (value) => value === MEMORY_FILTER_ALL || present.has(value),
    );
  }, [memories]);

  const filteredMemories = useMemo(() => {
    if (memoryType === MEMORY_FILTER_ALL) {
      return memories;
    }
    // 若所选类别当前没有数据（例如该类别记忆刚被删除），回落到"全部"，
    // 避免停留在一个注定为空的筛选上。
    if (!memories.some((m) => m.category === memoryType)) {
      return memories;
    }
    return memories.filter((m) => m.category === memoryType);
  }, [memories, memoryType]);

  const pendingReminders = useMemo(
    () =>
      reminders
        .filter((reminder) => reminder.status === "pending")
        .sort((a, b) => {
          const ta = new Date(a.remind_at).getTime();
          const tb = new Date(b.remind_at).getTime();
          const na = Number.isNaN(ta) ? 0 : ta;
          const nb = Number.isNaN(tb) ? 0 : tb;
          return na - nb;
        })
        .slice(0, 5),
    [reminders],
  );

  const serviceResults = services.slice(0, 5);

  const runWeather = () => {
    weatherMutation.mutate(
      { city: weatherCity.trim() || null, idempotency_key: `ui-weather-${Date.now()}` },
      {
        onError: (err) => window.alert(err instanceof Error ? err.message : "天气查询失败。"),
      },
    );
  };

  const runMap = () => {
    mapMutation.mutate(
      { query: mapQuery.trim(), idempotency_key: `ui-map-${Date.now()}` },
      {
        onError: (err) => window.alert(err instanceof Error ? err.message : "地图查询失败。"),
      },
    );
  };

  const saveMemory = () => {
    createMemory.mutate(
      { text: memoryDraft.trim(), category: saveCategory },
      {
        onSuccess: () => setMemoryDraft(""),
        onError: (err) => window.alert(err instanceof Error ? err.message : "无法保存记忆。"),
      },
    );
  };

  const loadError = isError
    ? error instanceof Error
      ? error.message
      : "无法加载 LifePilot 数据。"
    : null;

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto overscroll-contain pb-6">
      <DailyBriefingCard />

      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold tracking-tight text-white">LifePilot 工作区</h1>
          <p className="mt-0.5 text-[13px] text-white/55">
            来自已连接账户的个人自动化、长期记忆、提醒与生活服务。
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Link
            href="/tasks"
            className="rounded-full border border-white/20 bg-white/10 px-3 py-1.5 text-[12px] font-medium text-white/90 transition hover:bg-white/15"
          >
            任务
          </Link>
          <Link
            href="/calendar"
            className="rounded-full border border-white/20 bg-white/10 px-3 py-1.5 text-[12px] font-medium text-white/90 transition hover:bg-white/15"
          >
            日历
          </Link>
          <Link
            href="/background-jobs"
            className="rounded-full border border-white/20 bg-white/10 px-3 py-1.5 text-[12px] font-medium text-white/90 transition hover:bg-white/15"
          >
            自动化
          </Link>
        </div>
      </div>

      {loadError ? (
        <div className="rounded-xl border border-rose-400/30 bg-rose-500/10 px-4 py-3 text-[13px] text-rose-100/95" role="alert">
          {loadError}{" "}
          <button type="button" className="font-medium underline" onClick={() => void refetch()}>
            重试
          </button>
        </div>
      ) : null}

      {isPending ? (
        <p className="animate-pulse text-[13px] text-white/45">正在加载你的 LifePilot 上下文…</p>
      ) : null}

      <SectionShell title="长期记忆">
        <form
          className="flex flex-col gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            if (memoryDraft.trim()) saveMemory();
          }}
        >
          <label className="sr-only" htmlFor="memory-input">
            记忆备注
          </label>
          <textarea
            id="memory-input"
            value={memoryDraft}
            onChange={(event) => setMemoryDraft(event.target.value)}
            placeholder="添加希望被记住的偏好、目标或规则，例如：周五上午保留给深度工作；避免午餐会议..."
            rows={3}
            className="resize-none rounded-lg border border-white/15 bg-white/5 px-3 py-2 text-[13px] text-white placeholder-white/30 outline-none transition focus:border-violet-400/50 focus:bg-white/8"
          />
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="flex items-center gap-1.5" role="radiogroup" aria-label="保存类别">
              {(["fact", "rule", "preference"] as const).map((c) => (
                <button
                  key={c}
                  type="button"
                  role="radio"
                  aria-checked={saveCategory === c}
                  onClick={() => setSaveCategory(c)}
                  className={`inline-flex items-center gap-1 rounded-full px-2.5 py-1 text-[11px] font-medium transition ${
                    saveCategory === c
                      ? "bg-violet-500/30 text-violet-100 ring-1 ring-violet-400/30"
                      : "bg-white/8 text-white/55 hover:bg-white/12 hover:text-white/80"
                  }`}
                >
                  {c === "rule" ? <IconShield className="h-3 w-3" /> : <IconBookmark className="h-3 w-3" />}
                  {MEMORY_TYPE_LABEL[c]}
                </button>
              ))}
            </div>
            <button
              type="submit"
              disabled={!memoryDraft.trim() || createMemory.isPending}
              className="shrink-0 rounded-lg bg-violet-600 px-3 py-1.5 text-[12px] font-medium text-white transition hover:bg-violet-500 disabled:opacity-50"
            >
              {createMemory.isPending ? "正在保存…" : "保存记忆"}
            </button>
          </div>
          <p className="text-[11px] text-white/40">规则类记忆会在规划器生成计划时自动注入。</p>
        </form>

        <div className="mt-4 flex items-center gap-2.5" aria-hidden>
          <span className="h-px flex-1 bg-white/10" />
          <span className="text-[10px] font-medium uppercase tracking-[0.12em] text-white/35">已保存记忆 · 筛选</span>
          <span className="h-px flex-1 bg-white/10" />
        </div>

        <div className="mt-3 flex flex-wrap items-center gap-1.5" role="radiogroup" aria-label="筛选记忆类型">
          {memoryFilterChips.map((type) => (
            <button
              key={type}
              type="button"
              role="radio"
              aria-checked={memoryType === type}
              onClick={() => setMemoryType(type)}
              className={`inline-flex items-center gap-1 rounded-full px-2.5 py-1 text-[11px] font-medium transition ${
                memoryType === type
                  ? "bg-sky-500/30 text-sky-100 ring-1 ring-sky-400/30"
                  : "bg-white/8 text-white/55 hover:bg-white/12 hover:text-white/80"
              }`}
            >
              {type === "rule" ? <IconShield className="h-3 w-3" /> : <IconBookmark className="h-3 w-3" />}
              {type === MEMORY_FILTER_ALL ? "全部" : (MEMORY_TYPE_LABEL[type] ?? type)}
            </button>
          ))}
        </div>
        <ul className="dark-panel-scroll mt-4 flex max-h-[300px] flex-col gap-2 overflow-y-auto pr-1">
          {filteredMemories.map((memory) => (
            <li key={memory.id} className="rounded-lg border border-white/10 bg-white/4 px-3 py-2">
              <div className="flex items-start justify-between gap-2">
                <p className="min-w-0 flex-1 break-words text-[12px] leading-relaxed text-white/75">{memory.text}</p>
                <button
                  type="button"
                  className="shrink-0 rounded-md px-1.5 py-0.5 text-[10px] text-white/40 transition hover:bg-rose-500/15 hover:text-rose-300"
                  onClick={() => {
                    if (window.confirm("删除这条记忆？")) deleteMemory.mutate(memory.id);
                  }}
                >
                  删除
                </button>
              </div>
              <p className="mt-1 flex items-center gap-1 text-[10px] uppercase tracking-wide text-white/35">
                {memory.category === "rule" ? (
                  <>
                    <IconShield className="h-3 w-3 text-white/30" />
                    规则
                  </>
                ) : memory.category === "fact" ? (
                  <>
                    <IconBookmark className="h-3 w-3 text-white/30" />
                    事实
                  </>
                ) : (
                  MEMORY_TYPE_LABEL[memory.category] ?? memory.category
                )}
              </p>
            </li>
          ))}
          {filteredMemories.length === 0 && !isPending ? (
            <li className="rounded-lg border border-dashed border-white/10 px-3 py-4 text-center text-[12px] text-white/35">
              {memoryType === "all"
                ? "还没有保存记忆。规则会被规划器自动使用。"
                : `暂无「${MEMORY_TYPE_LABEL[memoryType] ?? memoryType}」分类的记忆。`}
            </li>
          ) : null}
        </ul>
      </SectionShell>

      <div className="grid gap-4 lg:grid-cols-2">
        <SectionShell title="主动提醒">
          {pendingReminders.length === 0 ? (
            <p className="rounded-lg border border-dashed border-white/10 px-3 py-6 text-center text-[12px] text-white/35">
              暂无待处理提醒。
            </p>
          ) : (
            <ul className="dark-panel-scroll flex max-h-[300px] flex-col gap-2 overflow-y-auto pr-1">
              {pendingReminders.map((reminder) => (
                <li key={reminder.id} className="rounded-lg border border-white/10 bg-white/4 px-3 py-2.5">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <span className="min-w-0 flex-1 text-[13px] font-medium text-white/90">{reminder.title}</span>
                    <span className="text-[11px] text-amber-200/70">{formatRelativeDate(reminder.remind_at)}</span>
                  </div>
                  <div className="mt-1 flex items-center gap-1.5">
                    <span
                      className={`rounded-full px-1.5 py-0.5 text-[10px] font-medium ${
                        reminder.trigger_type === "deadline"
                          ? "bg-violet-500/15 text-violet-200/90"
                          : "bg-sky-500/15 text-sky-200/90"
                      }`}
                    >
                      {reminder.trigger_type === "deadline" ? "任务截止" : "定时提醒"}
                    </span>
                    <span className="text-[10px] text-white/40">
                      {reminder.trigger_type === "deadline" ? "提前提醒" : "一次性"}
                    </span>
                  </div>
                  {reminder.note ? <p className="mt-1 line-clamp-2 text-[12px] text-white/50">{reminder.note}</p> : null}
                </li>
              ))}
            </ul>
          )}
        </SectionShell>

        <SectionShell title="生活服务">
          <div className="flex flex-col gap-4">
            <form
              className="flex flex-wrap items-end gap-2"
              onSubmit={(event) => {
                event.preventDefault();
                runWeather();
              }}
            >
              <div className="flex min-w-40 flex-1 flex-col gap-1">
                <label className="text-[11px] font-medium text-white/50" htmlFor="weather-city">
                  天气城市
                </label>
                <input
                  id="weather-city"
                  value={weatherCity}
                  onChange={(event) => setWeatherCity(event.target.value)}
                  placeholder="例如：上海"
                  className="rounded-lg border border-white/15 bg-white/5 px-3 py-2 text-[13px] text-white placeholder-white/30 outline-none transition focus:border-violet-400/50 focus:bg-white/8"
                />
              </div>
              <button
                type="submit"
                disabled={weatherMutation.isPending}
                className="inline-flex items-center gap-1.5 rounded-lg bg-white/10 px-3 py-2 text-[12px] font-medium text-white/90 transition hover:bg-white/15 disabled:opacity-50"
              >
                <IconWeather className="h-4 w-4" />
                {weatherMutation.isPending ? "正在查询…" : "查询天气"}
              </button>
            </form>

            <form
              className="flex flex-wrap items-end gap-2"
              onSubmit={(event) => {
                event.preventDefault();
                runMap();
              }}
            >
              <div className="flex min-w-40 flex-1 flex-col gap-1">
                <label className="text-[11px] font-medium text-white/50" htmlFor="map-query">
                  地图查询
                </label>
                <input
                  id="map-query"
                  value={mapQuery}
                  onChange={(event) => setMapQuery(event.target.value)}
                  placeholder="例如：附近咖啡"
                  className="rounded-lg border border-white/15 bg-white/5 px-3 py-2 text-[13px] text-white placeholder-white/30 outline-none transition focus:border-violet-400/50 focus:bg-white/8"
                />
              </div>
              <button
                type="submit"
                disabled={!mapQuery.trim() || mapMutation.isPending}
                className="inline-flex items-center gap-1.5 rounded-lg bg-white/10 px-3 py-2 text-[12px] font-medium text-white/90 transition hover:bg-white/15 disabled:opacity-50"
              >
                <IconMap className="h-4 w-4" />
                {mapMutation.isPending ? "正在搜索…" : "搜索地图"}
              </button>
            </form>

            <div className="flex flex-wrap gap-2">
              <StarterButton icon={<IconWeather className="h-4 w-4" />} label="让规划器查天气" prompt="本周规划时应如何考虑天气？" />
              <StarterButton icon={<IconMap className="h-4 w-4" />} label="让规划器查地图" prompt="帮我找附近可作为备用会议场地的地点。" />
            </div>
          </div>
        </SectionShell>
      </div>

      {serviceResults.length > 0 ? (
        <SectionShell title="生活服务记录">
          <ul className="dark-panel-scroll flex max-h-[300px] flex-col gap-2 overflow-y-auto pr-1">
            {serviceResults.map((entry) => (
              <li key={entry.id} className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-white/10 bg-white/4 px-3 py-2.5">
                <div className="min-w-0 flex-1">
                  <p className="truncate text-[13px] font-medium text-white/90">{entry.service_name}</p>
                  <p className="mt-0.5 truncate text-[11px] text-white/45">
                    {JSON.stringify(entry.result ?? entry.idempotency_key ?? entry.status)}
                  </p>
                </div>
                <span className={`rounded-md px-2 py-0.5 text-[11px] font-medium ${entry.status === "success" ? "bg-emerald-500/15 text-emerald-200/90" : "bg-rose-500/15 text-rose-200/90"}`}>
                  {entry.status}
                </span>
              </li>
            ))}
          </ul>
        </SectionShell>
      ) : null}

      <SectionShell title="快捷操作">
        <div className="flex flex-wrap gap-2">
          <StarterButton icon={<IconCalendar className="h-4 w-4" />} label="询问会议" prompt="我最近有哪些会议？请帮我列出来。" />
          <StarterButton icon={<IconBell className="h-4 w-4" />} label="规划提醒" prompt="为我下一次会议规划提醒。" />
        </div>
      </SectionShell>
    </div>
  );
}