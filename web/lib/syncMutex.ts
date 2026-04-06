/**
 * Sync mutex — coalesces concurrent calls into a single execution.
 *
 * When multiple callers invoke the returned function simultaneously,
 * only one underlying `fn` runs. All callers receive the same result.
 * After `fn` completes, the next call starts a new execution.
 */

type SyncResult = { ok: boolean; stderr: string };

export function createSyncMutex(
  fn: () => Promise<SyncResult>,
): () => Promise<SyncResult> {
  let inflight: Promise<SyncResult> | null = null;

  return () => {
    if (inflight) return inflight;
    inflight = fn().finally(() => {
      inflight = null;
    });
    return inflight;
  };
}
