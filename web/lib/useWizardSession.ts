"use client";

import { useEffect, useRef, useState } from "react";

export type WizardSessionState = {
  session_id: string;
  state: string;
  structure_name?: string;
  [key: string]: unknown;
};

export type UseWizardSessionResult = {
  session: WizardSessionState | null;
  loading: boolean;
  error: string | null;
};

export function useWizardSession(sessionId: string | null): UseWizardSessionResult {
  const [session, setSession] = useState<WizardSessionState | null>(null);
  const [loading, setLoading] = useState(Boolean(sessionId));
  const [error, setError] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    if (!sessionId) {
      setSession(null);
      setLoading(false);
      setError(null);
      return;
    }

    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setLoading(true);
    setError(null);

    const run = async () => {
      try {
        const response = await fetch(`/api/wizard/stream?session_id=${encodeURIComponent(sessionId)}`, {
          cache: "no-store",
          headers: { Accept: "text/event-stream" },
          signal: controller.signal,
        });
        if (!response.ok) {
          const body = await response.text().catch(() => "");
          throw new Error(`wizard stream ${response.status}: ${body.slice(0, 200)}`);
        }
        if (!response.body) {
          throw new Error("no response body for wizard SSE stream");
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";

        try {
          for (;;) {
            const { done, value } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            const events = buffer.split(/\n\n|\r\n\r\n/);
            buffer = events.pop() ?? "";

            for (const event of events) {
              if (!event.trim()) continue;
              const lines = event.split(/\r?\n/);
              let data = "";
              for (const line of lines) {
                if (line.startsWith("data: ")) data += line.slice(6);
              }
              if (!data) continue;
              try {
                const parsed = JSON.parse(data) as WizardSessionState;
                setSession(parsed);
                setLoading(false);
              } catch {
                // Skip malformed SSE payloads.
              }
            }
          }
        } finally {
          reader.releaseLock();
        }
      } catch (streamError) {
        if (streamError instanceof Error && streamError.name === "AbortError") {
          return;
        }
        setError(streamError instanceof Error ? streamError.message : "Unknown error");
        setLoading(false);
      }
    };

    void run();
    return () => controller.abort();
  }, [sessionId]);

  return { session, loading, error };
}
