"use client";

import Image from "next/image";
import { memo, useEffect, useRef } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

import { ReasoningBlock } from "./reasoning-block";
import { useTypewriter } from "@/hooks/use-typewriter";
import { normalizeRangeTildes } from "@/lib/markdown-text";

interface AssistantMessageProps {
  /** Message id — lets the parent pass one stable callback for every bubble. */
  messageId?: string;
  text: string;
  /** When true the text is revealed progressively with the orb animation. */
  shouldAnimate: boolean;
  /** Fired once the typewriter finishes so the parent can mark the message as "done". */
  onTypingComplete?: (messageId: string) => void;
  /** Voice/TTS turns: show literal text, do not parse markdown. */
  isPlainText?: boolean;
  /**
   * Intermediate text captured while the turn was streaming, shown collapsed above
   * the answer. Kept in memory only — a refresh shows just the final answer.
   */
  reasoning?: string;
  /** Streaming wall-clock time, rendered in the collapsed header. */
  reasoningDurationMs?: number;
}

const EXTERNAL_LINK_RE = /^https?:\/\//i;

/**
 * Model output can contain arbitrary links. Opening one in the same tab hands
 * the destination page control of this app (tab-nabbing), so every absolute
 * link opens in a new tab with `noopener noreferrer`.
 *
 * Defined at module scope: a fresh object per render remounts every link and
 * defeats the memo below.
 */
const MARKDOWN_COMPONENTS: Components = {
  // Only the attributes we mean to render are forwarded: react-markdown also
  // passes its AST `node`, which would otherwise leak onto the DOM element.
  a({ href, title, children }) {
    if (typeof href === "string" && EXTERNAL_LINK_RE.test(href)) {
      return (
        <a href={href} title={title} target="_blank" rel="noopener noreferrer">
          {children}
        </a>
      );
    }
    return (
      <a href={href} title={title}>
        {children}
      </a>
    );
  },
};

function InlineOrb() {
  return (
    <Image
      src="/assets/orb.png"
      alt=""
      width={20}
      height={20}
      aria-hidden
      className="ml-1 inline-block h-5 w-5 animate-orb-spin object-contain align-middle drop-shadow-[0_0_10px_rgba(168,85,247,0.65)]"
    />
  );
}

export const AssistantMessage = memo(function AssistantMessage({
  messageId,
  text,
  shouldAnimate,
  onTypingComplete,
  isPlainText = false,
  reasoning,
  reasoningDurationMs,
}: AssistantMessageProps) {
  const { displayedText, isTyping } = useTypewriter(text, shouldAnimate, messageId);
  const hasNotifiedRef = useRef(false);
  // `17~19℃` 里成对的 `~` 会被当成删除线，把整句划掉；渲染前先换「至」。
  const markdownText = isPlainText ? displayedText : normalizeRangeTildes(displayedText);
  const hasReasoning = typeof reasoning === "string" && reasoning.trim().length > 0;

  useEffect(() => {
    if (!isTyping && shouldAnimate && !hasNotifiedRef.current) {
      hasNotifiedRef.current = true;
      if (messageId) {
        onTypingComplete?.(messageId);
      }
    }
  }, [isTyping, shouldAnimate, onTypingComplete, messageId]);

  return (
    <div className="flex justify-start px-1 sm:px-2">
      <div className="assistant-markdown max-w-[min(90%,40rem)] px-1 py-1 text-left text-[15px] leading-relaxed text-white/95">
        {hasReasoning ? (
          <ReasoningBlock content={reasoning} durationMs={reasoningDurationMs} />
        ) : null}

        {isPlainText ? (
          <p className="whitespace-pre-wrap">{displayedText}</p>
        ) : (
          <ReactMarkdown remarkPlugins={[remarkGfm]} components={MARKDOWN_COMPONENTS}>
            {markdownText}
          </ReactMarkdown>
        )}

        {isTyping && <InlineOrb />}
      </div>
    </div>
  );
});

/**
 * The bubble that is still receiving SSE tokens. Deliberately NOT memoized:
 * its text changes on every delta, so a memo comparison is pure overhead, and
 * the streaming node must stay mounted while new chunks arrive.
 *
 * Renders as the expanded "thinking" panel: until `done` arrives there is no
 * final answer yet, so this muted, collapsible block is what the user reads.
 */
export function StreamingAssistantMessage({ text }: { text: string }) {
  return (
    <div className="flex justify-start px-1 sm:px-2">
      <div className="max-w-[min(90%,40rem)] px-1 py-1 text-left">
        <ReasoningBlock content={text} isStreaming />
      </div>
    </div>
  );
}
