"""Scanner-side R2 → local mirror sync.

Gated by meta/last_updated.json. If local mirror is as fresh as R2, skip.
Otherwise parallel-download all parquet/historical and parquet/indicators
for the requested timeframes, plus meta/universe.json. Atomic: downloads into
<mirror_dir>.tmp/ and swaps only after the full set arrives (A13). On partial
failure, leaves mirror untouched (A14). On R2 outage with an existing local
mirror, returns synced=False with a warning (A15).
"""

from __future__ import annotations

import json
import logging
import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

_SUPPORTED_SCHEMA_VERSIONS = {1}
_LAST_SYNC_FILE = ".last_sync.json"


class SchemaVersionError(RuntimeError):
    """Remote manifest carries a schema version we don't know how to read."""


@dataclass
class SyncResult:
    synced: bool
    stale_reason: str | None = None
    files_downloaded: int = 0
    errors: list[str] = field(default_factory=list)


def _load_local_last_sync(mirror_dir: Path) -> dict:
    path = mirror_dir / _LAST_SYNC_FILE
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        logger.warning("Corrupt %s; treating as empty", path)
        return {}


def _is_stale(local: dict, remote: dict) -> str | None:
    for key in ("historical", "indicators"):
        lv = local.get(key)
        rv = remote.get(key)
        if rv is None:
            return f"remote missing {key}"
        if lv is None or lv < rv:
            return f"{key} stale (local={lv}, remote={rv})"
    return None


def _download_prefix(r2, prefix: str, target_root: Path, max_workers: int) -> tuple[int, list[str]]:
    """Download every object under `prefix` into `target_root / key`.

    T6: thread-safe via return-value accumulation rather than nonlocal counter.
    `errors.append(...)` on a plain list is thread-safe in CPython (bytecode-level
    list.append via the GIL), so no lock is needed there.
    """
    keys = [k for k, _size, _mtime in r2.list_objects(prefix)]
    errors: list[str] = []

    def _one(key: str) -> int:
        try:
            body = r2.get_object(key)
            target = target_root / key
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
            return 1
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{key}: {exc}")
            return 0

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        downloaded = sum(ex.map(_one, keys))

    return downloaded, errors


def sync_if_stale(
    *,
    mirror_dir: Path = Path("data/apex_mirror"),
    r2=None,
    timeframes: tuple[str, ...] = ("1d", "1h"),
    force: bool = False,
    max_workers: int = 10,
) -> SyncResult:
    from scripts.ta_lib.r2_store import R2Error

    mirror_dir = Path(mirror_dir)
    if r2 is None:
        from scripts.ta_lib.r2_store import R2Store

        r2 = R2Store()

    # A15: R2 outage fallback
    try:
        remote = r2.get_json("meta/last_updated.json")
    except R2Error as exc:
        if (mirror_dir / "meta" / "universe.json").exists():
            logger.warning("R2 unreachable; using local mirror: %s", exc)
            return SyncResult(
                synced=False,
                errors=[f"R2 unreachable, using local mirror: {exc}"],
            )
        raise

    schema = remote.get("schema_version")
    if schema not in _SUPPORTED_SCHEMA_VERSIONS:
        raise SchemaVersionError(f"remote schema_version={schema}, supported={_SUPPORTED_SCHEMA_VERSIONS}")

    local = _load_local_last_sync(mirror_dir)
    stale = "force=True" if force else _is_stale(local, remote)
    if stale is None:
        return SyncResult(synced=False)

    logger.info("apex mirror stale: %s; syncing", stale)

    # A13: set up .tmp sibling with a copy of the existing mirror
    tmp_dir = mirror_dir.with_name(mirror_dir.name + ".tmp")
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    if mirror_dir.exists():
        shutil.copytree(mirror_dir, tmp_dir, dirs_exist_ok=True)
    else:
        tmp_dir.mkdir(parents=True)

    total_files = 0
    all_errors: list[str] = []
    try:
        for tf in timeframes:
            for kind in ("historical", "indicators"):
                prefix = f"parquet/{kind}/{tf}/"
                n, errs = _download_prefix(r2, prefix, tmp_dir, max_workers)
                total_files += n
                all_errors.extend(errs)

        # Always refresh universe.json — Stage A joins against it
        (tmp_dir / "meta").mkdir(parents=True, exist_ok=True)
        try:
            (tmp_dir / "meta" / "universe.json").write_bytes(r2.get_object("meta/universe.json"))
            total_files += 1
        except Exception as exc:  # noqa: BLE001
            all_errors.append(f"meta/universe.json: {exc}")

        # A14: don't swap on partial failure
        if all_errors:
            logger.warning(
                "Partial sync failure (%d errors); leaving mirror untouched",
                len(all_errors),
            )
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return SyncResult(synced=False, stale_reason=stale, errors=all_errors)

        # Write .last_sync.json INSIDE the tmp dir so it swaps atomically
        (tmp_dir / _LAST_SYNC_FILE).write_text(
            json.dumps(
                {
                    "historical": remote.get("historical"),
                    "indicators": remote.get("indicators"),
                    "schema_version": remote.get("schema_version"),
                }
            )
        )

        # A13: atomic swap via rename
        old_dir = mirror_dir.with_name(mirror_dir.name + ".old")
        if old_dir.exists():
            shutil.rmtree(old_dir)
        if mirror_dir.exists():
            mirror_dir.rename(old_dir)
        tmp_dir.rename(mirror_dir)
        if old_dir.exists():
            shutil.rmtree(old_dir, ignore_errors=True)

    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise

    return SyncResult(synced=True, stale_reason=stale, files_downloaded=total_files, errors=all_errors)
