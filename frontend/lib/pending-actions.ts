"use client";

import { authenticatedFetch } from "@/lib/authenticated-fetch";

export type PendingActionType = "email_send" | "email_reply" | "task_update" | "task_delete";

export type PendingActionStatus = "pending" | "approved" | "rejected" | "failed" | "expired";

export interface PendingActionRecord {
  id: string;
  actionType: PendingActionType;
  status: PendingActionStatus;
  title: string;
  summary: string | null;
  payload: Record<string, unknown>;
  createdAt: string | null;
  expiresAt: string | null;
}

/** Subjects this similar are treated as the same intent ("团建天气" vs "团建天气通知"). */
const SUBJECT_SIMILARITY_THRESHOLD = 0.85;

function normalizeText(value: unknown): string {
  return String(value ?? "").replace(/[\s\W_]+/gu, "").toLowerCase();
}

/** Character-overlap ratio; a cheap stand-in for edit distance on short subjects. */
function textSimilarity(left: string, right: string): number {
  if (left === right) return 1;
  if (!left || !right) return 0;
  const small = left.length <= right.length ? left : right;
  const large = left.length <= right.length ? right : left;
  if (large.includes(small) && small.length >= 4) return 1;
  const counts = new Map<string, number>();
  for (const char of small) counts.set(char, (counts.get(char) ?? 0) + 1);
  let shared = 0;
  for (const char of large) {
    const remaining = counts.get(char) ?? 0;
    if (remaining > 0) {
      shared += 1;
      counts.set(char, remaining - 1);
    }
  }
  return (2 * shared) / (left.length + right.length);
}

/**
 * Two drafts describe the same intent when they target the same object with
 * essentially the same content. The backend also merges duplicates, but SSE
 * retries can replay an already-rendered event, so the UI must guard too.
 */
export function isSamePendingActionIntent(left: PendingActionRecord, right: PendingActionRecord): boolean {
  if (left.id === right.id) return true;
  if (left.actionType !== right.actionType) return false;
  if (left.actionType === "email_send" || left.actionType === "email_reply") {
    const sameRecipient = normalizeText(left.payload.to) === normalizeText(right.payload.to);
    const subjectSimilarity = textSimilarity(
      normalizeText(left.payload.subject),
      normalizeText(right.payload.subject),
    );
    return sameRecipient && subjectSimilarity >= SUBJECT_SIMILARITY_THRESHOLD;
  }
  if (left.actionType === "task_delete" || left.actionType === "task_update") {
    return normalizeText(left.payload.task_id) === normalizeText(right.payload.task_id);
  }
  return false;
}

/** Insert or replace a draft, dropping older duplicates of the same intent. */
export function upsertPendingAction(
  existing: PendingActionRecord[],
  incoming: PendingActionRecord,
): PendingActionRecord[] {
  const withoutDuplicate = existing.filter((item) => !isSamePendingActionIntent(item, incoming));
  return [...withoutDuplicate, incoming];
}

/** Collapse a replayed/duplicated stream into one card per intent, keeping the newest. */
export function dedupePendingActions(actions: PendingActionRecord[]): PendingActionRecord[] {
  const result: PendingActionRecord[] = [];
  for (const action of actions) {
    const duplicateIndex = result.findIndex((item) => isSamePendingActionIntent(item, action));
    if (duplicateIndex >= 0) {
      result[duplicateIndex] = action;
      continue;
    }
    result.push(action);
  }
  return result;
}

interface ApproveResult {
  status: string;
  result: Record<string, unknown> | null;
}

const API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8081";

/** Confirm and execute a HITL draft. Returns the backend result or throws. */
export async function approvePendingAction(actionId: string): Promise<ApproveResult> {
  const response = await authenticatedFetch(`${API_BASE_URL}/pending-actions/${actionId}/approve`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
  });
  if (response.status === 404) {
    throw new Error("该确认操作不存在。");
  }
  if (response.status === 410) {
    const detail = await response.text();
    throw new Error(detail || "该确认操作已过期或状态不允许确认。");
  }
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `确认执行失败（${response.status}）`);
  }
  return (await response.json()) as ApproveResult;
}

/** Reject/decline a HITL draft without executing it. */
export async function rejectPendingAction(actionId: string): Promise<void> {
  const response = await authenticatedFetch(`${API_BASE_URL}/pending-actions/${actionId}/reject`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `拒绝操作失败（${response.status}）`);
  }
}