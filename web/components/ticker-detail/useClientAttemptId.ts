"use client";
import { useCallback, useEffect, useRef, useState } from "react";

// crypto.randomUUID is gated behind "secure context" — HTTPS, localhost,
// 127.0.0.1. Plain-HTTP LAN access (e.g. Tailscale MagicDNS at
// http://hostname:3000) is non-secure, so the API is undefined and throws.
// client_attempt_id is an idempotency key, not a security token — a
// Math.random-based v4 is unique enough.
function randomAttemptId(): string {
  const c = globalThis.crypto;
  if (c && typeof c.randomUUID === "function") {
    return c.randomUUID();
  }
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (ch) => {
    const r = (Math.random() * 16) | 0;
    const v = ch === "x" ? r : (r & 0x3) | 0x8;
    return v.toString(16);
  });
}

type Options = { ticker: string };

export function useClientAttemptId({ ticker }: Options) {
  const [id, setId] = useState(() => randomAttemptId());
  const submitted = useRef(false);
  const first = useRef(true);

  useEffect(() => {
    if (first.current) {
      first.current = false;
      return;
    }
    setId(randomAttemptId());
    submitted.current = false;
  }, [ticker]);

  const markSubmitted = useCallback(() => {
    submitted.current = true;
  }, []);
  const markTerminal = useCallback(() => {
    setId(randomAttemptId());
    submitted.current = false;
  }, []);
  const onFieldEdit = useCallback((_field: string) => {
    if (submitted.current) {
      setId(randomAttemptId());
      submitted.current = false;
    }
  }, []);

  return { id, markSubmitted, markTerminal, onFieldEdit };
}
