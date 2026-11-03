"use client";

import { useDailyBriefing, useRefreshBriefing } from "@/hooks/use-daily-briefing";

function IconRefresh({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.75} aria-hidden>
      <path strokeLinecap="round" strokeLinejoin="round" d="M4 4v5h5M20 20v-5h-5M5.5 10a7 7 0 0112-3.5M4 9l2-2m14 8l-2 2M18.5 14a7 7 0 01-12 3.5" />
    </svg>
  );
}

function BriefingSkeleton() {
  return (
    <div className="space-y-3" aria-hidden>
      <div className="h-4 w-2/3 animate-pulse rounded-full bg-white/15" />
      <div className="space-y-2">
        <div className="h-3 w-1/2 animate-pulse rounded-full bg-white/10" />
        <div className="h-3 w-full animate-pulse rounded-full bg-white/10" />
        <div className="h-3 w-5/6 animate-pulse rounded-full bg-white/10" />
        <div className="h-3 w-4/5 animate-pulse rounded-full bg-white/10" />
      </div>
    </div>
  );
}

const SECTION_HEADING_RE = /^【[^】]{1,12}】$/;

interface BriefingSection {
  heading: string | null;
  lines: string[];
}

/**
 * 晨报分两段展示（今日待办 / 已完成）。后端已经保证标题存在，
 * 这里按标题切块，让两段在视觉上真正分开，而不是一坨纯文本。
 */
function splitBriefingSections(content: string): BriefingSection[] {
  const sections: BriefingSection[] = [];
  let current: BriefingSection = { heading: null, lines: [] };
  for (const rawLine of content.split("\n")) {
    const line = rawLine.trimEnd();
    if (SECTION_HEADING_RE.test(line.trim())) {
      if (current.heading || current.lines.length > 0) sections.push(current);
      current = { heading: line.trim(), lines: [] };
      continue;
    }
    current.lines.push(line);
  }
  if (current.heading || current.lines.length > 0) sections.push(current);
  return sections.filter((section) => section.heading !== null || section.lines.join("").trim());
}

function BriefingContent({ content }: { content: string }) {
  const sections = splitBriefingSections(content);
  if (sections.length <= 1) {
    return <div className="whitespace-pre-line text-[13px] leading-relaxed text-white/85">{content}</div>;
  }
  return (
    <div className="space-y-3">
      {sections.map((section, index) => (
        <div
          key={`${section.heading ?? "intro"}-${index}`}
          className="rounded-xl border border-white/8 bg-white/4 px-3 py-2"
        >
          {section.heading ? (
            <p className="mb-1 text-[11px] font-semibold tracking-wide text-violet-200/85">
              {section.heading}
            </p>
          ) : null}
          <div className="whitespace-pre-line text-[13px] leading-relaxed text-white/85">
            {section.lines.join("\n").trim()}
          </div>
        </div>
      ))}
    </div>
  );
}

export function DailyBriefingCard() {
  const { data, isPending, isError, refetch } = useDailyBriefing();
  const refresh = useRefreshBriefing();

  const onRefresh = () => {
    refresh.mutate(undefined, {
      onSuccess: () => void refetch(),
      onError: (err) => window.alert(err instanceof Error ? err.message : "无法刷新今日晨报。"),
    });
  };

  return (
    <section className="relative rounded-2xl p-px bg-gradient-to-r from-violet-400/60 via-fuchsia-400/40 to-amber-300/50 shadow-[0_8px_30px_rgba(0,0,0,0.25)]">
      <div className="flex flex-col gap-3 rounded-[calc(1rem-1px)] border border-white/10 bg-black/45 p-4 backdrop-blur-md sm:p-5">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-2">
            <h2 className="text-[15px] font-semibold tracking-tight text-white">今日晨报</h2>
            {data?.date ? (
              <span className="rounded-full border border-white/10 bg-white/8 px-2 py-0.5 text-[11px] text-white/55">
                {data.date}
              </span>
            ) : null}
          </div>
          <button
            type="button"
            onClick={onRefresh}
            disabled={isPending || refresh.isPending}
            className="inline-flex items-center gap-1.5 rounded-lg border border-white/15 bg-white/10 px-2.5 py-1.5 text-[12px] font-medium text-white/85 transition hover:bg-white/15 disabled:opacity-50"
          >
            <IconRefresh className="h-3.5 w-3.5" />
            {refresh.isPending ? "刷新中…" : "刷新"}
          </button>
        </div>

        {isPending ? (
          <BriefingSkeleton />
        ) : isError || !data ? (
          <p className="text-[13px] text-rose-200/90">
            今日晨报加载失败。
            <button type="button" className="font-medium underline" onClick={() => void refetch()}>
              重试
            </button>
          </p>
        ) : (
          <BriefingContent content={data.content} />
        )}
      </div>
    </section>
  );
}