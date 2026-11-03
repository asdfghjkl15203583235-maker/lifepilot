"use client";

import { format } from "date-fns";
import { useCallback, useState, type KeyboardEvent } from "react";

import { RunTraceModal } from "./run-trace-modal";
import {
  RUN_OUTCOME_FAILED,
  RUN_OUTCOME_LABEL,
  RUN_OUTCOME_SKIPPED,
  RUN_OUTCOME_SUCCESS,
  type JobRun,
  type RunOutcome,
} from "@/lib/mock-background-jobs";

const OUTCOME_BADGE_CLASS: Record<RunOutcome, string> = {
  [RUN_OUTCOME_SUCCESS]: "border-emerald-400/35 bg-emerald-500/15 text-emerald-100",
  [RUN_OUTCOME_FAILED]: "border-rose-400/35 bg-rose-500/15 text-rose-100",
  [RUN_OUTCOME_SKIPPED]: "border-white/15 bg-white/8 text-white/75",
};

const MS_PER_SECOND = 1000;

interface BackgroundJobsHistorySectionProps {
  readonly runs: readonly JobRun[];
}

export function BackgroundJobsHistorySection({ runs }: BackgroundJobsHistorySectionProps) {
  const [selectedRun, setSelectedRun] = useState<JobRun | null>(null);

  const handleRowKeyDown = useCallback((event: KeyboardEvent<HTMLTableRowElement>, run: JobRun) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      setSelectedRun(run);
    }
  }, []);

  return (
    <section aria-labelledby="bg-jobs-history-heading" className="flex min-h-0 flex-col gap-3">
      <div>
        <h2 id="bg-jobs-history-heading" className="text-sm font-semibold text-white/90">
          运行历史
        </h2>
        <p className="mt-0.5 text-[13px] text-white/50">
          选择一行查看代理追踪：意图路由、工具、审批和输出步骤。
        </p>
      </div>

      <div className="overflow-x-auto rounded-xl border border-white/12 bg-black/30 shadow-[inset_0_1px_0_rgba(255,255,255,0.05)]">
        <table className="min-w-[720px] w-full border-collapse text-left text-[13px]">
          <thead>
            <tr className="border-b border-white/10 text-[11px] font-semibold uppercase tracking-wide text-white/45">
              <th scope="col" className="px-4 py-3 font-medium">
                任务
              </th>
              <th scope="col" className="px-4 py-3 font-medium">
                开始时间
              </th>
              <th scope="col" className="px-4 py-3 font-medium">
                时长
              </th>
              <th scope="col" className="px-4 py-3 font-medium">
                结果
              </th>
              <th scope="col" className="px-4 py-3 font-medium">
                详情
              </th>
            </tr>
          </thead>
          <tbody className="divide-y divide-white/8">
            {runs.length === 0 ? (
              <tr>
                <td colSpan={5} className="px-4 py-10 text-center text-[13px] text-white/40">
                  暂无运行记录。定时任务被执行后会在这里显示每次的代理追踪。
                </td>
              </tr>
            ) : (
              runs.map((run) => (
              <tr
                key={run.id}
                role="button"
                tabIndex={0}
                className="cursor-pointer align-top text-white/85 transition hover:bg-white/6 focus-visible:bg-white/8 focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-violet-400/80"
                onClick={() => setSelectedRun(run)}
                onKeyDown={(event) => handleRowKeyDown(event, run)}
              >
                <td className="max-w-[240px] px-4 py-3">
                  <p className="font-medium text-white">{run.jobTitle}</p>
                  <p className="mt-0.5 font-mono text-[11px] text-white/35">{run.jobId}</p>
                </td>
                <td className="whitespace-nowrap px-4 py-3 tabular-nums text-white/78">
                  {format(new Date(run.startedAtIso), "MMM d · h:mm:ss a")}
                </td>
                <td className="whitespace-nowrap px-4 py-3 tabular-nums text-white/78">
                  {(run.durationMs / MS_PER_SECOND).toFixed(1)}s
                </td>
                <td className="px-4 py-3">
                  <span
                    className={`inline-flex rounded-full border px-2.5 py-0.5 text-[11px] font-medium ${OUTCOME_BADGE_CLASS[run.outcome]}`}
                  >
                    {RUN_OUTCOME_LABEL[run.outcome]}
                  </span>
                </td>
                <td
                  className="max-w-[320px] px-4 py-3 text-[12px] leading-snug text-white/65"
                  title={run.detail}
                >
                  <span className="line-clamp-2">{run.detail}</span>
                </td>
              </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      <RunTraceModal run={selectedRun} onClose={() => setSelectedRun(null)} />
    </section>
  );
}
