"use client";

import { BackgroundJobsScheduledSection } from "./background-jobs-scheduled-section";
import { BackgroundJobsHistorySection } from "./background-jobs-history-section";
import { BackgroundJobsStats } from "./background-jobs-stats";
import {
  useBackgroundJobsQuery,
  useDeleteBackgroundJobMutation,
  useStopBackgroundJobMutation,
} from "@/hooks/use-background-jobs";
import { MOCK_BACKGROUND_JOBS, MOCK_JOB_RUNS } from "@/lib/mock-background-jobs";

const STATUS_PULSE_CLASS = "animate-pulse";

export function BackgroundJobsDashboard() {
  const { data, isPending, isFetching, isError, error, refetch } = useBackgroundJobsQuery();
  const stopJob = useStopBackgroundJobMutation();
  const deleteJob = useDeleteBackgroundJobMutation();

  // While the first request is in flight, `data` is undefined — do not fall back to mock data
  // (that made every refresh look like demo data). Mocks are only for failed API loads.
  const displayJobs = isError ? MOCK_BACKGROUND_JOBS : (data?.jobs ?? []);
  const displayRuns = isError ? MOCK_JOB_RUNS : (data?.runs ?? []);
  const totalRunsThisWeek = displayRuns.length;
  const isLive = Boolean(!isError && data);
  const isInitialLoading = isPending && !isError;
  const isStatusLoading = isInitialLoading || (isFetching && !data);
  const queryErrorMessage =
    isError && error instanceof Error ? error.message : isError ? "请求失败" : null;

  const onStop = (jobId: string) => {
    stopJob.mutate(jobId, {
      onError: (err) => {
        window.alert(err instanceof Error ? err.message : "无法停止该定时任务。");
      },
    });
  };

  const onDelete = (jobId: string) => {
    deleteJob.mutate(jobId, {
      onError: (err) => {
        window.alert(err instanceof Error ? err.message : "无法删除该定时任务。");
      },
    });
  };

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-10 overflow-y-auto overflow-x-hidden pb-10 pt-1">
      <div
        className={`shrink-0 rounded-xl border border-violet-400/25 bg-violet-500/10 px-4 py-3 text-[13px] text-white/85 ${
          isStatusLoading ? STATUS_PULSE_CLASS : ""
        }`}
        role="status"
      >
        <p className="font-medium text-white">
          {isInitialLoading ? "正在加载定时任务…" : isLive ? "定时任务与运行记录" : "后台任务"}
        </p>
        <p className="mt-1 text-white/65">
          {isLive
            ? "数据已从你登录账户关联的 API 加载。"
            : queryErrorMessage
              ? `无法从 API 加载（${queryErrorMessage}）。当前显示演示数据。 `
              : isInitialLoading
                ? "正在获取你的定时任务和运行历史。"
                : "演示数据。 "}
          {queryErrorMessage ? (
            <button
              type="button"
              className="ml-0 font-medium text-emerald-200 underline decoration-emerald-400/50"
              onClick={() => {
                void refetch();
              }}
            >
              重试
            </button>
          ) : null}
        </p>
      </div>

      <div
        className={`flex min-h-0 flex-1 flex-col gap-10 ${isInitialLoading ? STATUS_PULSE_CLASS : ""}`}
      >
        <div className="shrink-0">
          <BackgroundJobsStats jobs={displayJobs} totalRunsThisWeek={totalRunsThisWeek} />
        </div>

        <div className="shrink-0 border-t border-white/10 pt-2">
          <BackgroundJobsScheduledSection
            jobs={displayJobs}
            canManage={isLive}
            onStopJob={onStop}
            onDeleteJob={onDelete}
            pendingStopJobId={stopJob.isPending && stopJob.variables != null ? stopJob.variables : null}
            pendingDeleteJobId={
              deleteJob.isPending && deleteJob.variables != null ? deleteJob.variables : null
            }
          />
        </div>

        <div className="shrink-0 border-t border-white/10 pt-2">
          <BackgroundJobsHistorySection runs={displayRuns} />
        </div>
      </div>
    </div>
  );
}
