"""Daily pre-market TA data preparation.

Audits the DuckDB TA cache for staleness, refreshes via IB, and reports
before/after coverage.  Designed to run before market open so trend_scan
has warm caches.

Usage::

    python3.13 scripts/ta_premarket_prep.py [--audit-only] [--force] [--db PATH]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Optional

_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from scripts.ta_lib.service import TAService
from scripts.ta_lib.store import get_connection, get_latest_bar_date, init_schema
from scripts.trend_scan_lib.config import TrendScanConfig
from scripts.trend_scan_lib.universe import build_static_universe, build_universe
from scripts.utils.market_calendar import get_last_n_trading_days

logger = logging.getLogger(__name__)

DEFAULT_DB = "data/ta.duckdb"
DEFAULT_SP500 = "data/universe/sp500.json"
DEFAULT_NASDAQ100 = "data/universe/nasdaq100.json"
UNIVERSE_CACHE = Path("data/ta_premarket_universe.json")


# ── Phase 1: Audit ──────────────────────────────────────────────────────


def _last_trading_date() -> date:
    """Return the most recent completed trading day."""
    day_str = get_last_n_trading_days(1)[0]
    return datetime.strptime(day_str, "%Y-%m-%d").date()


def classify_tickers(
    conn,
    tickers: list[str],
    ref_date: date,  # noqa: ARG001 — unused; _is_stale uses its own session-anchored cutoff
) -> dict[str, list[str]]:
    """Classify tickers as current / stale / missing.

    Delegates the freshness decision to TAService._is_stale(), which uses
    its own ET-aware "last completed trading session" logic. The staleness
    cutoff is therefore independent of ref_date.

    ref_date is retained for API compatibility — main() passes it, and
    callers may depend on the signature — but it does not drive the
    staleness check.
    """
    current: list[str] = []
    stale: list[str] = []
    missing: list[str] = []

    # _svc is constructed on first use to avoid the TAService import and
    # read_only() call when every ticker is missing (e.g. empty DB on first run).
    _svc = None

    def _get_svc():
        nonlocal _svc
        if _svc is None:
            _svc = TAService.read_only(conn)
        return _svc

    for t in tickers:
        latest = get_latest_bar_date(conn, t, "1d")
        if latest is None:
            missing.append(t)
            continue
        # Use _is_stale() — this catches "bars present but no indicators"
        # and applies the same ET-aware logic scanners use.
        if _get_svc()._is_stale(t, "1d", cursor=conn):
            stale.append(t)
        else:
            current.append(t)

    return {"current": current, "stale": stale, "missing": missing}


def _print_audit(label: str, cls: dict[str, list[str]]) -> None:
    total = sum(len(v) for v in cls.values())
    print(
        f"[{label}] current={len(cls['current'])}  "
        f"stale={len(cls['stale'])}  missing={len(cls['missing'])}  "
        f"total={total}",
        file=sys.stderr,
    )
    if cls["stale"]:
        print(f"  stale: {', '.join(cls['stale'][:20])}", file=sys.stderr)
    if cls["missing"]:
        print(f"  missing: {', '.join(cls['missing'][:20])}", file=sys.stderr)


def _counts(cls: dict[str, list[str]]) -> dict[str, int]:
    return {k: len(v) for k, v in cls.items()}


# ── Universe helpers ─────────────────────────────────────────────────────


def _connect_uw_client():
    """Return a UW client or None if unavailable — non-fatal."""
    try:
        from scripts.clients.uw_client import UWClient

        return UWClient()
    except Exception as exc:
        logger.warning("UW client unavailable: %s — prep will omit UW flow tickers", exc)
        return None


def _connect_ib_client():
    """Return an IB client or None if unavailable — non-fatal."""
    try:
        from scripts.clients.ib_client import IBClient

        ib = IBClient()
        ib.connect(client_id="auto")
        return ib
    except Exception as exc:
        logger.warning("IB client unavailable: %s — prep will skip IB scanner universe", exc)
        return None


def _build_triple_source_universe(
    cfg: TrendScanConfig,
) -> tuple[list[str], object | None, object | None]:
    """Build the full scanner universe (refresh mode only).

    Never called from --audit-only. Uses real TrendScanConfig defaults so
    prep sees the same UW lookback / premium threshold the scanner does.
    Returns (tickers, uw_client_or_None, ib_client_or_None). The IB client
    (if returned) is kept open for reuse by the TAService refresh phase.
    """
    uw = _connect_uw_client()
    ib = _connect_ib_client()
    try:
        tickers = build_universe(cfg, uw_client=uw, ib_client=ib)
    except Exception as exc:
        logger.warning("build_universe failed (%s) — falling back to static-only", exc)
        tickers = build_static_universe(
            sp500_path=cfg.sp500_path,
            nasdaq100_path=cfg.nasdaq100_path,
        )
    if "SPY" not in tickers:
        tickers.append("SPY")
    return tickers, uw, ib


def _build_static_only_universe(args) -> list[str]:
    """Build static-only universe for --audit-only. Never touches network."""
    tickers = build_static_universe(sp500_path=args.sp500, nasdaq100_path=args.nasdaq100)
    if "SPY" not in tickers:
        tickers.append("SPY")
    return tickers


# ── Main ─────────────────────────────────────────────────────────────────


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Pre-market TA data prep")
    parser.add_argument("--audit-only", action="store_true", help="Audit only, no IB refresh. Offline.")
    parser.add_argument("--force", action="store_true", help="Refresh all tickers, not just stale/missing")
    parser.add_argument("--db", default=DEFAULT_DB, help="DuckDB path")
    parser.add_argument("--sp500", default=DEFAULT_SP500, help="SP500 universe JSON")
    parser.add_argument("--nasdaq100", default=DEFAULT_NASDAQ100, help="NASDAQ100 universe JSON")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    conn = get_connection(args.db)
    init_schema(conn)
    ref_date = _last_trading_date()

    if args.audit_only:
        tickers = _build_static_only_universe(args)
        before = classify_tickers(conn, tickers, ref_date)
        _print_audit("BEFORE", before)
        json.dump({"before": _counts(before)}, sys.stdout, indent=2)
        print(file=sys.stdout)
        return

    # Refresh mode: build the FULL scanner universe via real TrendScanConfig.
    cfg = TrendScanConfig(sp500_path=args.sp500, nasdaq100_path=args.nasdaq100)
    tickers, uw_client, ib_client_preopened = _build_triple_source_universe(cfg)

    # Persist so the 8:30 AM scan can reuse the exact same universe.
    UNIVERSE_CACHE.parent.mkdir(parents=True, exist_ok=True)
    UNIVERSE_CACHE.write_text(
        json.dumps(
            {
                "tickers": tickers,
                "built_at": datetime.now().isoformat(timespec="seconds"),
                "source_counts": {
                    "total": len(tickers),
                    "has_uw": uw_client is not None,
                    "has_ib_scanner": ib_client_preopened is not None,
                },
            },
            indent=2,
        )
    )

    before = classify_tickers(conn, tickers, ref_date)
    _print_audit("BEFORE", before)

    t0 = time.monotonic()
    if ib_client_preopened is not None:
        ib = ib_client_preopened
    else:
        try:
            from scripts.clients.ib_client import IBClient

            ib = IBClient()
            ib.connect(client_id="auto")
        except Exception as exc:
            logger.warning("IB connection failed: %s — skipping refresh", exc)
            json.dump({"before": _counts(before), "error": str(exc)}, sys.stdout, indent=2)
            print(file=sys.stdout)
            return

    ta_svc = TAService(db_path=args.db, ib_client=ib)

    refresh_tickers = tickers if args.force else before["stale"] + before["missing"]
    if refresh_tickers:
        logger.info("Refreshing %d tickers ...", len(refresh_tickers))
        ta_svc.bulk_refresh(refresh_tickers)

    elapsed = time.monotonic() - t0
    after = classify_tickers(conn, tickers, ref_date)
    _print_audit("AFTER", after)

    failed = sorted(set(after["stale"] + after["missing"]) & set(refresh_tickers))
    result = {
        "before": _counts(before),
        "after": _counts(after),
        "refreshed": len(refresh_tickers),
        "failed_tickers": failed,
        "elapsed_s": round(elapsed, 1),
    }
    json.dump(result, sys.stdout, indent=2)
    print(file=sys.stdout)

    try:
        ib.disconnect()
    except Exception:
        pass


if __name__ == "__main__":
    main()
