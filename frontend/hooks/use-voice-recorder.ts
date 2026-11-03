"use client";

import { useCallback, useEffect, useRef, useState } from "react";

export const VOICE_WAVEFORM_BAR_COUNT = 56;

const ANALYSER_FFT_SIZE = 512;
const LEVEL_SMOOTHING = 0.62;
const MIN_VISIBLE_LEVEL = 0.06;
const MEDIA_RECORDER_TIMESLICE_MS = 120;
const ELAPSED_TICK_MS = 100;

const MEDIA_RECORDER_MIME_CANDIDATES = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4"] as const;

function pickMediaRecorderMimeType(): string | undefined {
  if (typeof MediaRecorder === "undefined" || !MediaRecorder.isTypeSupported) {
    return undefined;
  }
  for (const candidate of MEDIA_RECORDER_MIME_CANDIDATES) {
    if (MediaRecorder.isTypeSupported(candidate)) {
      return candidate;
    }
  }
  return undefined;
}

function mapFrequencyDataToBars(data: Uint8Array, barCount: number): number[] {
  const binCount = data.length;
  const levels: number[] = [];
  for (let index = 0; index < barCount; index += 1) {
    const t = index / Math.max(barCount - 1, 1);
    const startBin = Math.floor(Math.pow(t, 1.55) * binCount * 0.88);
    const nextT = (index + 1) / Math.max(barCount - 1, 1);
    const endBin = Math.min(Math.ceil(Math.pow(nextT, 1.55) * binCount * 0.88), binCount);
    let peak = 0;
    for (let b = startBin; b < endBin; b += 1) {
      const value = data[b];
      if (value !== undefined) {
        peak = Math.max(peak, value / 255);
      }
    }
    levels.push(peak);
  }
  return levels;
}

function userMediaErrorMessage(error: unknown): string {
  if (error instanceof DOMException) {
    if (error.name === "NotAllowedError") {
      return "麦克风访问被拒绝。请在浏览器设置中允许麦克风后重试。";
    }
    if (error.name === "NotFoundError") {
      return "未找到麦克风。";
    }
  }
  return "无法访问麦克风。";
}

function createInitialLevels(barCount: number): number[] {
  return Array.from({ length: barCount }, () => MIN_VISIBLE_LEVEL);
}

export interface UseVoiceRecorderResult {
  levels: number[];
  isRecording: boolean;
  errorMessage: string | null;
  elapsedMs: number;
  startRecording: () => Promise<void>;
  cancelRecording: () => void;
  finishRecording: () => Promise<Blob | null>;
}

export function useVoiceRecorder(barCount: number = VOICE_WAVEFORM_BAR_COUNT): UseVoiceRecorderResult {
  const [levels, setLevels] = useState<number[]>(() => createInitialLevels(barCount));
  const [isRecording, setIsRecording] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [elapsedMs, setElapsedMs] = useState(0);

  const streamRef = useRef<MediaStream | null>(null);
  const audioContextRef = useRef<AudioContext | null>(null);
  const analyserRef = useRef<AnalyserNode | null>(null);
  const frequencyBufferRef = useRef<Uint8Array | null>(null);
  const smoothedLevelsRef = useRef<number[]>(createInitialLevels(barCount));
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const animationFrameRef = useRef<number | null>(null);
  const startedAtRef = useRef<number | null>(null);
  const elapsedIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const startRequestInFlightRef = useRef(false);

  const cancelAnimationLoop = useCallback(() => {
    if (animationFrameRef.current !== null) {
      cancelAnimationFrame(animationFrameRef.current);
      animationFrameRef.current = null;
    }
  }, []);

  const stopElapsedTicker = useCallback(() => {
    if (elapsedIntervalRef.current !== null) {
      clearInterval(elapsedIntervalRef.current);
      elapsedIntervalRef.current = null;
    }
    startedAtRef.current = null;
  }, []);

  const releaseStreamAndContext = useCallback(async () => {
    cancelAnimationLoop();
    streamRef.current?.getTracks().forEach((track) => {
      track.stop();
    });
    streamRef.current = null;
    analyserRef.current = null;
    frequencyBufferRef.current = null;

    const context = audioContextRef.current;
    audioContextRef.current = null;
    if (context && context.state !== "closed") {
      await context.close();
    }

    mediaRecorderRef.current = null;
    chunksRef.current = [];
  }, [cancelAnimationLoop]);

  const runLevelLoop = useCallback(() => {
    const step = () => {
      const analyser = analyserRef.current;
      const buffer = frequencyBufferRef.current;
      if (!analyser || !buffer) {
        return;
      }
      // TypeScript 5+ `Uint8Array` is `ArrayBufferLike`-backed; Web Audio `getByteFrequencyData` types expect `ArrayBuffer`.
      analyser.getByteFrequencyData(buffer as Uint8Array<ArrayBuffer>);
      const raw = mapFrequencyDataToBars(buffer, barCount);
      const previous = smoothedLevelsRef.current;
      const next = raw.map((value, index) => {
        const prev = previous[index] ?? MIN_VISIBLE_LEVEL;
        const blended = prev * LEVEL_SMOOTHING + value * (1 - LEVEL_SMOOTHING);
        return Math.max(MIN_VISIBLE_LEVEL, blended);
      });
      smoothedLevelsRef.current = next;
      setLevels([...next]);
      animationFrameRef.current = requestAnimationFrame(step);
    };
    animationFrameRef.current = requestAnimationFrame(step);
  }, [barCount]);

  const startRecording = useCallback(async () => {
    setErrorMessage(null);
    if (startRequestInFlightRef.current || mediaRecorderRef.current?.state === "recording") {
      return;
    }
    if (typeof navigator === "undefined" || !navigator.mediaDevices?.getUserMedia) {
      setErrorMessage("当前浏览器不支持语音录制。");
      return;
    }

    startRequestInFlightRef.current = true;
    await releaseStreamAndContext();

    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
        },
      });
      streamRef.current = stream;

      const audioContext = new AudioContext();
      await audioContext.resume();
      audioContextRef.current = audioContext;

      const analyser = audioContext.createAnalyser();
      analyser.fftSize = ANALYSER_FFT_SIZE;
      analyser.smoothingTimeConstant = 0.42;
      analyserRef.current = analyser;

      const source = audioContext.createMediaStreamSource(stream);
      source.connect(analyser);

      const bufferLength = analyser.frequencyBinCount;
      frequencyBufferRef.current = new Uint8Array(bufferLength);
      smoothedLevelsRef.current = createInitialLevels(barCount);
      setLevels([...smoothedLevelsRef.current]);

      const mimeType = pickMediaRecorderMimeType();
      const recorder = mimeType ? new MediaRecorder(stream, { mimeType }) : new MediaRecorder(stream);
      chunksRef.current = [];
      recorder.ondataavailable = (event: BlobEvent) => {
        if (event.data.size > 0) {
          chunksRef.current.push(event.data);
        }
      };

      mediaRecorderRef.current = recorder;
      recorder.start(MEDIA_RECORDER_TIMESLICE_MS);

      const startedAt = Date.now();
      startedAtRef.current = startedAt;
      setElapsedMs(0);
      elapsedIntervalRef.current = setInterval(() => {
        if (startedAtRef.current !== null) {
          setElapsedMs(Date.now() - startedAtRef.current);
        }
      }, ELAPSED_TICK_MS);

      setIsRecording(true);
      runLevelLoop();
    } catch (error) {
      await releaseStreamAndContext();
      setErrorMessage(userMediaErrorMessage(error));
      setLevels(createInitialLevels(barCount));
    } finally {
      startRequestInFlightRef.current = false;
    }
  }, [barCount, releaseStreamAndContext, runLevelLoop]);

  const cancelRecording = useCallback(async () => {
    stopElapsedTicker();
    cancelAnimationLoop();
    setElapsedMs(0);

    const recorder = mediaRecorderRef.current;
    if (recorder && recorder.state !== "inactive") {
      await new Promise<void>((resolve) => {
        recorder.addEventListener(
          "stop",
          () => {
            resolve();
          },
          { once: true },
        );
        recorder.stop();
      });
    }

    await releaseStreamAndContext();
    smoothedLevelsRef.current = createInitialLevels(barCount);
    setLevels(createInitialLevels(barCount));
    setIsRecording(false);
  }, [barCount, cancelAnimationLoop, releaseStreamAndContext, stopElapsedTicker]);

  const finishRecording = useCallback(async (): Promise<Blob | null> => {
    stopElapsedTicker();
    cancelAnimationLoop();
    setElapsedMs(0);

    const recorder = mediaRecorderRef.current;

    if (!recorder || recorder.state === "inactive") {
      await releaseStreamAndContext();
      setIsRecording(false);
      smoothedLevelsRef.current = createInitialLevels(barCount);
      setLevels(createInitialLevels(barCount));
      return null;
    }

    const blob = await new Promise<Blob | null>((resolve) => {
      recorder.addEventListener(
        "stop",
        () => {
          const parts = chunksRef.current.slice();
          const type = recorder.mimeType || "audio/webm";
          const built = new Blob(parts, { type });
          resolve(built.size > 0 ? built : null);
        },
        { once: true },
      );
      recorder.stop();
    });

    await releaseStreamAndContext();

    smoothedLevelsRef.current = createInitialLevels(barCount);
    setLevels(createInitialLevels(barCount));
    setIsRecording(false);

    return blob;
  }, [barCount, cancelAnimationLoop, releaseStreamAndContext, stopElapsedTicker]);

  useEffect(() => {
    return () => {
      stopElapsedTicker();
      cancelAnimationLoop();
      void releaseStreamAndContext();
    };
  }, [cancelAnimationLoop, releaseStreamAndContext, stopElapsedTicker]);

  return {
    levels,
    isRecording,
    errorMessage,
    elapsedMs,
    startRecording,
    cancelRecording,
    finishRecording,
  };
}
