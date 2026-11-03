"use client";

import { useEffect } from "react";

/** App-level error boundary for the workspace tree. Catches render errors in any
 *  dashboard panel and shows a retry card instead of a white screen. */
export default function AppError({
  error,
  unstable_retry,
}: {
  error: Error & { digest?: string };
  unstable_retry: () => void;
}) {
  useEffect(() => {
    console.error(error);
  }, [error]);

  return (
    <div className="flex h-dvh items-center justify-center bg-zinc-950 px-6">
      <div className="w-full max-w-md rounded-2xl border border-rose-400/25 bg-black/55 px-6 py-8 text-center shadow-[inset_0_1px_0_rgba(255,255,255,0.06)] backdrop-blur-md">
        <p className="text-[11px] font-semibold uppercase tracking-[0.14em] text-white/40">
          LifePilot
        </p>
        <h1 className="mt-1 text-lg font-semibold text-white">页面组件出错</h1>
        <p className="mt-2 text-[13px] leading-relaxed text-white/60">
          某个界面组件遇到意外错误，已阻止整页白屏。可以重试，或返回工作区。
        </p>
        <div className="mt-5 flex flex-wrap justify-center gap-2">
          <button
            type="button"
            onClick={() => unstable_retry()}
            className="rounded-full border border-white/20 bg-white/10 px-4 py-2 text-[13px] font-medium text-white transition hover:bg-white/15"
          >
            重试
          </button>
        </div>
      </div>
    </div>
  );
}