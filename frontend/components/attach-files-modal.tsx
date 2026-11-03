"use client";

import {
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
  type ChangeEvent,
  type DragEvent,
} from "react";
import { createPortal } from "react-dom";

const ATTACH_MODAL_TITLE = "添加附件";
const ATTACH_MODAL_DROP_ZONE_LABEL = "上传文件";
const ATTACH_MODAL_DROP_HINT_PRIMARY = "拖放文件到这里";
const ATTACH_MODAL_DROP_HINT_SECONDARY = "或点击浏览";
const ATTACH_MODAL_CLOSE_ARIA_LABEL = "关闭添加附件对话框";
const ATTACH_FILE_INPUT_ACCEPT = "*/*";

function fileListToFiles(fileList: FileList | null): File[] {
  if (!fileList || fileList.length === 0) {
    return [];
  }
  return Array.from(fileList);
}

function IconUploadCloud({ className }: { className?: string }) {
  return (
    <svg
      className={className}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.75}
      aria-hidden
    >
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M7 16a4 4 0 01-.88-7.903A5 5 0 1115.9 6L16 6a5 5 0 011 9.9M15 13l-3-3m0 0l-3 3m3-3v12"
      />
    </svg>
  );
}

function IconCloseModal({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" aria-hidden>
      <path
        stroke="currentColor"
        strokeWidth={2}
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M18 6L6 18M6 6l12 12"
      />
    </svg>
  );
}

export interface AttachFilesModalProps {
  isOpen: boolean;
  onClose: () => void;
  onFilesSelected?: (files: File[]) => void;
}

export function AttachFilesModal({
  isOpen,
  onClose,
  onFilesSelected,
}: AttachFilesModalProps) {
  const titleId = useId();
  const fileInputReference = useRef<HTMLInputElement>(null);
  const dragDepthReference = useRef(0);
  const [isDraggingOver, setIsDraggingOver] = useState(false);

  const handleClose = useCallback(() => {
    dragDepthReference.current = 0;
    setIsDraggingOver(false);
    onClose();
  }, [onClose]);

  useEffect(() => {
    if (!isOpen) {
      return;
    }

    const handleEscape = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape") {
        handleClose();
      }
    };

    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    document.addEventListener("keydown", handleEscape);

    return () => {
      document.body.style.overflow = previousOverflow;
      document.removeEventListener("keydown", handleEscape);
    };
  }, [isOpen, handleClose]);

  const finalizeFiles = useCallback(
    (files: File[]) => {
      if (files.length === 0) {
        return;
      }
      if (fileInputReference.current) {
        fileInputReference.current.value = "";
      }
      onFilesSelected?.(files);
      handleClose();
    },
    [handleClose, onFilesSelected],
  );

  const handleInputChange = useCallback(
    (event: ChangeEvent<HTMLInputElement>) => {
      finalizeFiles(fileListToFiles(event.target.files));
    },
    [finalizeFiles],
  );

  const handleDropZoneClick = useCallback(() => {
    fileInputReference.current?.click();
  }, []);

  const handleDragEnter = useCallback((event: DragEvent<HTMLButtonElement>) => {
    event.preventDefault();
    event.stopPropagation();
    dragDepthReference.current += 1;
    setIsDraggingOver(true);
  }, []);

  const handleDragLeave = useCallback((event: DragEvent<HTMLButtonElement>) => {
    event.preventDefault();
    event.stopPropagation();
    dragDepthReference.current -= 1;
    if (dragDepthReference.current <= 0) {
      dragDepthReference.current = 0;
      setIsDraggingOver(false);
    }
  }, []);

  const handleDragOver = useCallback((event: DragEvent<HTMLButtonElement>) => {
    event.preventDefault();
    event.stopPropagation();
    event.dataTransfer.dropEffect = "copy";
  }, []);

  const handleDrop = useCallback(
    (event: DragEvent<HTMLButtonElement>) => {
      event.preventDefault();
      event.stopPropagation();
      dragDepthReference.current = 0;
      setIsDraggingOver(false);
      finalizeFiles(fileListToFiles(event.dataTransfer.files));
    },
    [finalizeFiles],
  );

  if (!isOpen) {
    return null;
  }

  if (typeof document === "undefined") {
    return null;
  }

  const dropZoneClassName = isDraggingOver
    ? "border-fuchsia-400/80 bg-fuchsia-500/15 ring-2 ring-fuchsia-400/50"
    : "border-white/20 bg-white/5 hover:border-white/30 hover:bg-white/10";

  const modal = (
    <div
      className="fixed inset-0 z-100 flex items-center justify-center p-4 sm:p-6"
      role="presentation"
    >
      <button
        type="button"
        aria-label="关闭"
        className="absolute inset-0 bg-black/65 backdrop-blur-sm"
        onClick={handleClose}
      />
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className="relative z-10 w-full max-w-lg rounded-2xl border border-white/15 bg-zinc-950/95 p-6 shadow-[0_24px_80px_-20px_rgba(0,0,0,0.9)] backdrop-blur-md"
      >
        <div className="mb-5 flex items-start justify-between gap-4">
          <h2 id={titleId} className="text-lg font-semibold tracking-tight text-white">
            {ATTACH_MODAL_TITLE}
          </h2>
          <button
            type="button"
            onClick={handleClose}
            aria-label={ATTACH_MODAL_CLOSE_ARIA_LABEL}
            className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl text-white/70 transition hover:bg-white/10 hover:text-white focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-fuchsia-400/80"
          >
            <IconCloseModal className="h-5 w-5" />
          </button>
        </div>

        <input
          ref={fileInputReference}
          type="file"
          multiple
          accept={ATTACH_FILE_INPUT_ACCEPT}
          className="sr-only"
          aria-hidden
          tabIndex={-1}
          onChange={handleInputChange}
        />

        <button
          type="button"
          aria-label={ATTACH_MODAL_DROP_ZONE_LABEL}
          className={`flex w-full cursor-pointer flex-col items-center justify-center rounded-xl border-2 border-dashed px-6 py-14 transition ${dropZoneClassName}`}
          onClick={handleDropZoneClick}
          onDragEnter={handleDragEnter}
          onDragLeave={handleDragLeave}
          onDragOver={handleDragOver}
          onDrop={handleDrop}
        >
          <IconUploadCloud className="mb-4 h-12 w-12 text-fuchsia-300/90" />
          <p className="text-center text-[15px] font-medium text-white/95">
            {ATTACH_MODAL_DROP_HINT_PRIMARY}
          </p>
          <p className="mt-2 text-center text-sm text-white/55">{ATTACH_MODAL_DROP_HINT_SECONDARY}</p>
        </button>
      </div>
    </div>
  );

  return createPortal(modal, document.body);
}
