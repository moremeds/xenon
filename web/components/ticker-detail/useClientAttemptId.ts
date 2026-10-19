"use client";
import { useCallback, useEffect, useRef, useState } from "react";

// crypto.randomUUID is gated behind "secure context" — HTTPS, localhost,
// 127.0.0.1. Plain-HTTP LAN access (e.g. Tailscale MagicDNS at
// http://hostname:3000) is non-secure, so the API is undefined and throws.
// client_attempt_id is an idempotency key, not a security token — a
// Math.random-based v4 is unique enough.
export function randomAttemptId(): string {
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
  // PLACE_OUTCOME_UNKNOWN (or any post-dispatch failure): IB may hold the
  // order. Sticky for this ticker: the id is never rotated again (not by a
  // later response, not by a field edit), and submitPlaceOrder refuses every
  // further submit. Only a new instrument (ticker change / remount) clears it.
  const uncertainRef = useRef(false);
  const [uncertain, setUncertain] = useState(false);

  useEffect(() => {
    if (first.current) {
      first.current = false;
      return;
    }
    setId(randomAttemptId());
    submitted.current = false;
    uncertainRef.current = false;
    setUncertain(false);
  }, [ticker]);

  const markSubmitted = useCallback(() => {
    submitted.current = true;
  }, []);
  const markTerminal = useCallback(() => {
    if (uncertainRef.current) return;
    setId(randomAttemptId());
    submitted.current = false;
  }, []);
  const markUncertain = useCallback(() => {
    uncertainRef.current = true;
    setUncertain(true);
  }, []);
  const isUncertain = useCallback(() => uncertainRef.current, []);
  const onFieldEdit = useCallback((_field: string) => {
    if (uncertainRef.current) return;
    if (submitted.current) {
      setId(randomAttemptId());
      submitted.current = false;
    }
  }, []);

  return { id, markSubmitted, markTerminal, markUncertain, isUncertain, uncertain, onFieldEdit };
}
