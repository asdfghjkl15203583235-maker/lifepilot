"use client";

import { useEffect, useRef, useState } from "react";

const CHARS_PER_TICK = 3;
const TICK_INTERVAL_MS = 18;

interface TypewriterResult {
  displayedText: string;
  isTyping: boolean;
}

/**
 * Progressively reveals `fullText` to simulate a streaming / typewriter effect.
 * Returns the currently visible slice and whether the animation is still running.
 *
 * Two rules keep it from replaying text the user has already seen:
 *
 * 1. The reveal starts from zero only when animation is *turned on* for a
 *    message (or a different message id arrives). Re-renders that merely hand
 *    over a longer `fullText` continue from the current position.
 * 2. If that longer `fullText` already extends past the revealed prefix (the
 *    text is being appended to, e.g. extra tokens after the reply finished),
 *    revealing continues from the current position instead of jumping back to
 *    zero and re-typing the whole answer.
 *
 * Without them a finished reply that keeps receiving tokens visibly clears
 * itself and types the whole answer out again.
 */
export function useTypewriter(
  fullText: string,
  shouldAnimate: boolean,
  messageId?: string,
): TypewriterResult {
  const [charIndex, setCharIndex] = useState(() => (shouldAnimate ? 0 : fullText.length));
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const charIndexRef = useRef(charIndex);
  /** Identity of the text currently being revealed; a change means "start over". */
  const activeKeyRef = useRef<string | null>(null);
  /** Previous render's inputs, to tell "just started" apart from "text grew". */
  const previousRef = useRef<{ text: string; animate: boolean }>({
    text: fullText,
    animate: shouldAnimate,
  });

  charIndexRef.current = charIndex;

  useEffect(() => {
    if (!shouldAnimate) {
      activeKeyRef.current = null;
      // Streaming bubbles live here: every SSE token grows `fullText`, so the
      // index must follow it or the bubble freezes at the first chunk.
      setCharIndex(fullText.length);
      return;
    }

    const key = messageId ?? "anonymous";
    const wasAnimating = previousRef.current.animate && activeKeyRef.current === key;
    const startedNow = !wasAnimating || activeKeyRef.current !== key;
    const previousText = previousRef.current.text;
    const resumedAt = previousText.length > 0 ? charIndexRef.current : 0;
    activeKeyRef.current = key;

    // Appending to already-revealed text: jump ahead and animate only the tail.
    // Otherwise start over — that is the intended "type the new reply out" case.
    const startAt = startedNow ? 0 : Math.min(resumedAt, fullText.length);

    setCharIndex(startAt);

    if (startAt >= fullText.length) {
      return;
    }

    intervalRef.current = setInterval(() => {
      setCharIndex((previous) => {
        const next = previous + CHARS_PER_TICK;
        if (next >= fullText.length) {
          if (intervalRef.current !== null) {
            clearInterval(intervalRef.current);
            intervalRef.current = null;
          }
          return fullText.length;
        }
        return next;
      });
    }, TICK_INTERVAL_MS);

    return () => {
      if (intervalRef.current !== null) {
        clearInterval(intervalRef.current);
        intervalRef.current = null;
      }
    };
  }, [fullText, shouldAnimate, messageId]);

  useEffect(() => {
    previousRef.current = { text: fullText, animate: shouldAnimate };
  }, [fullText, shouldAnimate]);

  const clampedIndex = Math.min(charIndex, fullText.length);
  const displayedText = fullText.slice(0, clampedIndex);
  const isTyping = clampedIndex < fullText.length;

  return { displayedText, isTyping };
}
