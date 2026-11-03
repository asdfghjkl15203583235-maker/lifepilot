"use client";

import { useEffect, useMemo, useState } from "react";

export interface ConversationSidebarItem {
  id: string;
  title: string;
  /** True when this id maps to a server-persisted conversation. */
  persisted: boolean;
  updatedAt?: string;
}

interface ConversationSidebarProps {
  items: ConversationSidebarItem[];
  activeConversationId: string;
  onSelect: (conversationId: string) => void;
  onNewChat: () => void;
  onDelete: (conversationId: string) => void;
  /** Mobile: whether the sidebar is expanded. */
  isExpanded: boolean;
  onToggleExpanded: () => void;
}

function IconPlus({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} aria-hidden>
      <path strokeLinecap="round" strokeLinejoin="round" d="M12 5v14M5 12h14" />
    </svg>
  );
}

function IconSearch({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.75} aria-hidden>
      <path strokeLinecap="round" strokeLinejoin="round" d="M21 21l-4.35-4.35M17 10a7 7 0 11-14 0 7 7 0 0114 0z" />
    </svg>
  );
}

function IconDotsHorizontal({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="currentColor" aria-hidden>
      <circle cx="5" cy="12" r="1.6" />
      <circle cx="12" cy="12" r="1.6" />
      <circle cx="19" cy="12" r="1.6" />
    </svg>
  );
}

function IconTrash({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.75} aria-hidden>
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M19 7l-.87 12.14A2 2 0 0116.14 21H7.86a2 2 0 01-1.99-1.86L5 7m5 4v6m4-6v6M10 7V5a2 2 0 012-2h0a2 2 0 012 2v2M4 7h16"
      />
    </svg>
  );
}

function IconChat({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.75} aria-hidden>
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M8 10h8m-8 4h5M21 12a9 9 0 01-12.9 8.1L3 21l.9-5.1A9 9 0 1121 12z"
      />
    </svg>
  );
}

export function ConversationSidebar({
  items,
  activeConversationId,
  onSelect,
  onNewChat,
  onDelete,
  isExpanded,
  onToggleExpanded,
}: ConversationSidebarProps) {
  const [query, setQuery] = useState("");
  const [menuOpenFor, setMenuOpenFor] = useState<string | null>(null);

  useEffect(() => {
    if (menuOpenFor === null) {
      return;
    }
    const handlePointerDown = (event: MouseEvent) => {
      if (!(event.target instanceof Element)) {
        return;
      }
      const withinMenu = event.target.closest(
        `[data-conversation-menu="${menuOpenFor}"]`,
      );
      if (!withinMenu) {
        setMenuOpenFor(null);
      }
    };
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setMenuOpenFor(null);
      }
    };
    document.addEventListener("mousedown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("mousedown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [menuOpenFor]);

  const filtered = useMemo(() => {
    const trimmed = query.trim().toLowerCase();
    if (!trimmed) {
      return items;
    }
    return items.filter((item) => item.title.toLowerCase().includes(trimmed));
  }, [items, query]);

  return (
    <div
      className={`shrink-0 overflow-hidden border-r border-white/10 bg-black/45 transition-all duration-300 ${
        isExpanded ? "w-64" : "w-12"
      }`}
    >
      <div className="flex h-full flex-col gap-2 p-2">
        <div className="flex shrink-0 items-center gap-1.5">
          <button
            type="button"
            onClick={onToggleExpanded}
            aria-label={isExpanded ? "收起会话侧边栏" : "展开会话侧边栏"}
            title={isExpanded ? "收起" : "展开"}
            className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-white/20 bg-white/12 text-white/85 transition hover:bg-white/22"
          >
            <IconChat className="h-4 w-4" />
          </button>

          {isExpanded ? (
            <button
              type="button"
              onClick={onNewChat}
              className="flex h-8 flex-1 items-center justify-center gap-1.5 rounded-lg border border-white/20 bg-white/14 text-[13px] font-medium text-white/90 transition hover:bg-white/22"
            >
              <IconPlus className="h-4 w-4" />
              新建对话
            </button>
          ) : null}
        </div>

        {isExpanded ? (
          <>
            <div className="relative shrink-0">
              <span className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-white/40">
                <IconSearch className="h-4 w-4" />
              </span>
              <input
                type="search"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="搜索会话…"
                aria-label="搜索会话"
                className="h-9 w-full rounded-lg border border-white/15 bg-black/35 pl-9 pr-3 text-[13px] text-white/90 placeholder:text-white/35 focus:border-violet-400/50 focus:outline-none"
              />
            </div>

            <nav className="min-h-0 flex-1 overflow-y-auto overscroll-contain" aria-label="会话列表">
              {filtered.length === 0 ? (
                <p className="px-2 py-6 text-center text-[12px] text-white/40">暂无会话</p>
              ) : (
                <ul className="flex flex-col gap-1">
                  {filtered.map((item) => {
                    const isActive = item.id === activeConversationId;
                    return (
                      <li
                        key={item.id}
                        className={`group flex items-center gap-1 rounded-lg transition ${
                          isActive ? "bg-white/16" : "hover:bg-white/10"
                        }`}
                      >
                        <button
                          type="button"
                          onClick={() => onSelect(item.id)}
                          aria-current={isActive ? "true" : undefined}
                          className="flex min-w-0 flex-1 items-center gap-2 px-2.5 py-2 text-left"
                        >
                          <IconChat className="h-4 w-4 shrink-0 text-white/45" />
                          <span
                            className={`truncate text-[13px] ${
                              isActive ? "font-medium text-white" : "text-white/78"
                            }`}
                          >
                            {item.title}
                          </span>
                        </button>
                        <div data-conversation-menu={item.id} className="relative mr-1 shrink-0">
                          <button
                            type="button"
                            aria-label={`更多操作：${item.title}`}
                            title="更多操作"
                            aria-expanded={menuOpenFor === item.id}
                            aria-haspopup="true"
                            onClick={(event) => {
                              event.stopPropagation();
                              setMenuOpenFor((open) => (open === item.id ? null : item.id));
                            }}
                            className="rounded-md p-1.5 text-white/40 opacity-0 transition hover:bg-white/15 hover:text-white group-hover:opacity-100"
                          >
                            <IconDotsHorizontal className="h-4 w-4" />
                          </button>
                          {menuOpenFor === item.id ? (
                            <div
                              role="menu"
                              className="absolute right-0 top-8 z-30 w-36 overflow-hidden rounded-lg border border-white/15 bg-zinc-900/95 py-1 shadow-xl backdrop-blur-md"
                              onClick={(event) => event.stopPropagation()}
                            >
                              <button
                                type="button"
                                role="menuitem"
                                onClick={() => {
                                  setMenuOpenFor(null);
                                  if (window.confirm(`确定删除「${item.title}」吗？删除后无法恢复。`)) {
                                    onDelete(item.id);
                                  }
                                }}
                                className="flex w-full items-center gap-2 px-3 py-2 text-left text-[13px] font-medium text-rose-300 transition hover:bg-rose-500/15 hover:text-rose-100"
                              >
                                <IconTrash className="h-4 w-4" />
                                删除会话
                              </button>
                            </div>
                          ) : null}
                        </div>
                      </li>
                    );
                  })}
                </ul>
              )}
            </nav>

            <p className="shrink-0 px-1 text-center text-[10px] text-white/30">按 thread 隔离 · 自动保存</p>
          </>
        ) : null}
      </div>
    </div>
  );
}