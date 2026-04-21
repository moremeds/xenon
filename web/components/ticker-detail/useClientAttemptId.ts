"use client";
import { useCallback, useEffect, useRef, useState } from "react";

type Options = { ticker: string };

export function useClientAttemptId({ ticker }: Options) {
  const [id, setId] = useState(() => crypto.randomUUID());
  const submitted = useRef(false);
  const first = useRef(true);

  useEffect(() => {
    if (first.current) {
      first.current = false;
      return;
    }
    setId(crypto.randomUUID());
    submitted.current = false;
  }, [ticker]);

  const markSubmitted = useCallback(() => {
    submitted.current = true;
  }, []);
  const markTerminal = useCallback(() => {
    setId(crypto.randomUUID());
    submitted.current = false;
  }, []);
  const onFieldEdit = useCallback((_field: string) => {
    if (submitted.current) {
      setId(crypto.randomUUID());
      submitted.current = false;
    }
  }, []);

  return { id, markSubmitted, markTerminal, onFieldEdit };
}
