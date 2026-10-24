import { useSyncExternalStore } from "react";

export type FutuAccountEnv = "paper" | "live";

const STORAGE_KEY = "xenon.futu.account_env";
const DEFAULT_ENV: FutuAccountEnv = "paper";

const listeners = new Set<(env: FutuAccountEnv) => void>();

export function getFutuAccountEnv(): FutuAccountEnv {
  if (typeof window === "undefined") return DEFAULT_ENV;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (raw === "paper" || raw === "live") return raw;
  } catch {
    // private mode / disabled storage
  }
  return DEFAULT_ENV;
}

export function setFutuAccountEnv(env: FutuAccountEnv): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, env);
  } catch {
    // ignore
  }
  for (const fn of listeners) fn(env);
}

export function subscribeFutuAccountEnv(
  onStoreChange: (env: FutuAccountEnv) => void,
): () => void {
  listeners.add(onStoreChange);
  return () => listeners.delete(onStoreChange);
}

/** Wire query for Futu-scoped API calls: `broker=FUTU&account_env=<env>`. */
export function futuQuery(env: FutuAccountEnv): string {
  return `broker=FUTU&account_env=${encodeURIComponent(env)}`;
}

/** `?broker=FUTU&account_env=…` for hook endpoints. */
export function futuScopeQuery(env: FutuAccountEnv): string {
  return `?${futuQuery(env)}`;
}

/** Account tab subtitle env line for the Futu card (paper → simulate). */
export function futuTabEnvironmentLabel(env: FutuAccountEnv): string {
  return env === "paper" ? "simulate" : "real";
}

export { forwardRequestQuery } from "./forwardRequestQuery";

export function useFutuAccountEnv(): FutuAccountEnv {
  return useSyncExternalStore(
    subscribeFutuAccountEnv,
    getFutuAccountEnv,
    () => DEFAULT_ENV,
  );
}
