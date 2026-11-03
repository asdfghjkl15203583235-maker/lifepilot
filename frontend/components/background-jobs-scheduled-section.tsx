"use client";

import { useMemo, useState } from "react";

import { ScheduleJobCard } from "./schedule-job-card";
import { ScheduleJobsAllModal } from "./schedule-jobs-all-modal";
import {
  isOngoingSchedule,
  type BackgroundJob,
} from "@/lib/mock-background-jobs";

const SCHEDULE_PREVIEW_CARD_LIMIT = 3;

function sortOngoingByNextRun(jobs: readonly BackgroundJob[]): BackgroundJob[] {
  return [...jobs].sort((jobA, jobB) => {
    const timeA = jobA.nextRunIso ? new Date(jobA.nextRunIso).getTime() : 0;
    const timeB = jobB.nextRunIso ? new Date(jobB.nextRunIso).getTime() : 0;
    return timeA - timeB;
  });
}

interface BackgroundJobsScheduledSectionProps {
  readonly jobs: readonly BackgroundJob[];
  readonly canManage?: boolean;
  readonly onStopJob?: (jobId: string) => void;
  readonly onDeleteJob?: (jobId: string) => void;
  readonly pendingStopJobId?: string | null;
  readonly pendingDeleteJobId?: string | null;
}

export function BackgroundJobsScheduledSection({
  jobs,
  canManage = false,
  onStopJob,
  onDeleteJob,
  pendingStopJobId = null,
  pendingDeleteJobId = null,
}: BackgroundJobsScheduledSectionProps) {
  const [isAllModalOpen, setIsAllModalOpen] = useState(false);

  const previewJobs = useMemo(() => {
    const ongoing = jobs.filter(isOngoingSchedule);
    return sortOngoingByNextRun(ongoing).slice(0, SCHEDULE_PREVIEW_CARD_LIMIT);
  }, [jobs]);

  return (
    <section aria-labelledby="bg-jobs-scheduled-heading" className="flex min-h-0 flex-col gap-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 id="bg-jobs-scheduled-heading" className="text-sm font-semibold text-white/90">
            定时任务
          </h2>
          <p className="mt-0.5 text-[13px] text-white/50">
            每个计划可以包含多个动作（邮件、日历、Notion 等）。只需告诉 LifePilot 一次，例如：“每天上午 10 点发送邮件给 x@mail.com”。
          </p>
        </div>
        <button
          type="button"
          onClick={() => setIsAllModalOpen(true)}
          className="shrink-0 rounded-full border border-white/20 bg-white/10 px-4 py-2 text-[13px] font-medium text-white/90 shadow-sm transition hover:border-white/30 hover:bg-white/16"
        >
          查看全部
        </button>
      </div>

      {previewJobs.length === 0 ? (
        <p className="rounded-xl border border-white/10 bg-black/25 px-4 py-8 text-center text-[13px] text-white/50">
          暂无即将运行的定时任务。
        </p>
      ) : (
        <ul className="grid gap-4 lg:grid-cols-3">
          {previewJobs.map((job) => (
            <li key={job.id}>
              <ScheduleJobCard
                job={job}
                density="comfortable"
                canManage={canManage}
                onStopJob={onStopJob}
                onDeleteJob={onDeleteJob}
                isStopPending={pendingStopJobId === job.id}
                isDeletePending={pendingDeleteJobId === job.id}
              />
            </li>
          ))}
        </ul>
      )}

      <ScheduleJobsAllModal
        isOpen={isAllModalOpen}
        jobs={jobs}
        onClose={() => setIsAllModalOpen(false)}
        canManage={canManage}
        onStopJob={onStopJob}
        onDeleteJob={onDeleteJob}
        pendingStopJobId={pendingStopJobId}
        pendingDeleteJobId={pendingDeleteJobId}
      />
    </section>
  );
}
