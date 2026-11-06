"use client";

import { useEffect, useRef, useState } from "react";

const SECONDS_PER_MINUTE = 60;
const DEFAULT_DURATION_LABEL = "数秒";

interface ReasoningBlockProps {
  /** The raw "process" text captured while the turn was running. */
  content: string;
  /** Wall-clock time from request start to the final answer, shown in the header. */
  durationMs?: number;
  /** While tokens are still arriving the block starts expanded and reads "思考中". */
  isStreaming?: boolean;
}

function formatDuration(durationMs?: number): string {
  if (durationMs === undefined || durationMs <= 0) {
    return DEFAULT_DURATION_LABEL;
  }
  const totalSeconds = Math.max(1, Math.round(durationMs / 1000));
  if (totalSeconds < SECONDS_PER_MINUTE) {
    return `${totalSeconds} 秒`;
  }
  const minutes = Math.floor(totalSeconds / SECONDS_PER_MINUTE);
  const seconds = totalSeconds % SECONDS_PER_MINUTE;
  return seconds === 0 ? `${minutes} 分钟` : `${minutes} 分 ${seconds} 秒`;
}

function IconChevron({ className }: { className?: string }) {
  return (
    <svg
      className={className}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      aria-hidden
    >
      <path strokeLinecap="round" strokeLinejoin="round" d="M6 9l6 6 6-6" />
    </svg>
  );
}

/**
 * Collapsible "thinking" panel above the answer, mirroring the DeepSeek web UI:
 * muted small text, collapsed by default once the answer is ready, expanded while
 * the turn is still streaming so the user always has something to read.
 */
export function ReasoningBlock({ content, durationMs, isStreaming = false }: ReasoningBlockProps) {
  // Seeded once per mount: the streaming bubble and the finished message are
  // separate instances, so each gets the default that fits its state.
  const [isOpen, setIsOpen] = useState(isStreaming);
  const contentRef = useRef<HTMLDivElement | null>(null);

  // Keep the newest delta visible while tokens are arriving; the panel is capped
  // in height, so without this the text scrolls past the bottom.
  useEffect(() => {
    if (!isOpen || !isStreaming) {
      return;
    }
    const node = contentRef.current;
    if (node !== null) {
      node.scrollTop = node.scrollHeight;
    }
  }, [content, isOpen, isStreaming]);

  if (content.trim().length === 0) {
    return null;
  }

  const headerLabel = isStreaming
    ? "思考中…"
    : `已深度思考（用时 ${formatDuration(durationMs)}）`;

  return (
    <div className="mb-2 overflow-hidden rounded-lg border border-white/10 bg-white/[0.03]">
      <button
        type="button"
        onClick={() => setIsOpen((open) => !open)}
        aria-expanded={isOpen}
        className="flex w-full items-center gap-2 px-3 py-2 text-left text-[12px] font-medium text-white/45 transition hover:text-white/70"
      >
        <span>{headerLabel}</span>
        <IconChevron
          className={`ml-auto h-3.5 w-3.5 shrink-0 transition-transform ${isOpen ? "rotate-180" : ""}`}
        />
      </button>
      {isOpen ? (
        <div
          ref={contentRef}
          className="max-h-72 overflow-y-auto overscroll-contain border-t border-white/10 px-3 py-2 text-[13px] leading-relaxed whitespace-pre-wrap text-white/45"
        >
          {content}
        </div>
      ) : null}
    </div>
  );
}
