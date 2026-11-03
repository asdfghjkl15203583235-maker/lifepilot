"use client";

import {
  JOB_STATUS_ACTIVE,
  JOB_STATUS_ENDED,
  JOB_STATUS_PAUSED,
  type BackgroundJob,
  countJobsByStatus,
} from "@/lib/mock-background-jobs";

const STATS_GRID_CLASS =
  "grid gap-3 sm:grid-cols-2 lg:grid-cols-4";

interface BackgroundJobsStatsProps {
  readonly jobs: readonly BackgroundJob[];
  readonly totalRunsThisWeek: number;
}

export function BackgroundJobsStats({ jobs, totalRunsThisWeek }: BackgroundJobsStatsProps) {
  const statusCounts = countJobsByStatus(jobs);
  const activeCount = statusCounts[JOB_STATUS_ACTIVE];
  const pausedCount = statusCounts[JOB_STATUS_PAUSED];
  const endedCount = statusCounts[JOB_STATUS_ENDED];

  const items = [
    { label: "运行中的定时任务", value: String(activeCount), hint: "按计划执行中" },
    { label: "已暂停", value: String(pausedCount), hint: "等待恢复" },
    { label: "已结束", value: String(endedCount), hint: "已归档任务" },
    { label: "运行次数（示例周）", value: String(totalRunsThisWeek), hint: "演示历史数量" },
  ];

  return (
    <section aria-labelledby="bg-jobs-stats-heading">
      <h2 id="bg-jobs-stats-heading" className="sr-only">
        后台任务统计
      </h2>
      <ul className={STATS_GRID_CLASS}>
        {items.map((item) => (
          <li
            key={item.label}
            className="rounded-xl border border-white/10 bg-black/35 px-4 py-3 shadow-[inset_0_1px_0_rgba(255,255,255,0.06)]"
          >
            <p className="text-[11px] font-medium uppercase tracking-wide text-white/45">{item.label}</p>
            <p className="mt-1 text-2xl font-semibold tabular-nums text-white">{item.value}</p>
            <p className="mt-1 text-[11px] text-white/38">{item.hint}</p>
          </li>
        ))}
      </ul>
    </section>
  );
}
