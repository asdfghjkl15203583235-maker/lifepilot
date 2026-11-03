"use client";

import { useEffect } from "react";

/** Root error boundary — replaces the whole app instead of white-screening when an
 *  unexpected render error bubbles past all nested boundaries. */
export default function GlobalError({
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
    <html lang="zh-CN">
      <body>
        <div
          style={{
            minHeight: "100vh",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            background: "#0a0a0f",
            color: "#e4e4e7",
            fontFamily: "system-ui, -apple-system, sans-serif",
          }}
        >
          <div style={{ maxWidth: 420, textAlign: "center", padding: "0 16px" }}>
            <p style={{ fontSize: 13, color: "#71717a", margin: "0 0 8px" }}>LifePilot</p>
            <h1 style={{ fontSize: 20, margin: "0 0 8px", fontWeight: 600 }}>应用遇到意外错误</h1>
            <p style={{ fontSize: 14, color: "#a1a1aa", margin: "0 0 20px", lineHeight: 1.6 }}>
              页面渲染出错，点击下方按钮重试。
            </p>
            <button
              type="button"
              onClick={() => unstable_retry()}
              style={{
                borderRadius: 9999,
                border: "1px solid rgba(255,255,255,0.2)",
                background: "rgba(255,255,255,0.1)",
                color: "#fff",
                padding: "10px 22px",
                fontSize: 14,
                fontWeight: 500,
                cursor: "pointer",
              }}
            >
              重新加载
            </button>
          </div>
        </div>
      </body>
    </html>
  );
}