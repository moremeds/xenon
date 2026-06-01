"""FutuClient — read-only positions + account info from Futu OpenD.

## Scope

v1: **positions + account snapshot only**. No order placement, no fills,
no market-data subscriptions. Futu is strictly observed through this
client — never written to.

## Design

- **Singleton-capable.** The intended consumer is FastAPI, which holds
  one long-lived instance in its app lifespan (like `ib_pool.py`). The
  class itself is not a literal singleton; wiring that is the caller's
  job.
- **Synchronous.** All methods block. The Futu SDK is fully sync and
  calling it from asyncio requires `run_in_executor`, which is the
  FastAPI wrapper's job — not this class's. Keeping this layer plain
  makes the CLI and the tests trivial.
- **Singleflight.** A reentrant lock serializes concurrent `fetch_*`
  calls so two simultaneous HTTP requests collapse to one OpenD roundtrip.
- **TTL cache + rate-limit cooldown.** Futu enforces 10 calls / 30s on
  both `position_list_query` and `accinfo_query`. Fresh cache is served
  without hitting OpenD; rate-limit hits activate a cooldown window
  during which cached data is served.
- **UTC timestamps at every boundary.** `fetched_at` and `data_as_of`
  are always ISO-Z UTC per the plan's timestamp rule.

## Why not reuse apex/adapter.py verbatim

Apex uses a domain model (`Position`, `AccountInfo` dataclasses) + event
bus + async interface. Xenon emits plain dicts directly to atomic JSON,
and has no event bus. This class is roughly apex's `PositionFetcher` +
`AccountFetcher` fused into one sync interface, with Xenon's time/symbol
normalization layered on.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from xenon.clients.futu_exceptions import (
    FutuConnectionError,
    FutuDataError,
    FutuError,
    FutuRateLimitError,
    classify_futu_exception,
)
from xenon.utils.symbol_norm import futu_to_ib
from xenon.utils.time_norm import iso_z, now_utc

# Module-level imports of futu SDK symbols. Previously imported lazily inside
# connect() — that prevented unittest.mock.patch from intercepting them
# (correction #8 from the perf-rebuild plan review). Failing futu-api means
# the client cannot connect; raising at import time would block downstream
# code that just imports the module. Hence the try/except fallback to None.
try:
    from futu import (
        RET_OK,
        OpenSecTradeContext,
        SecurityFirm,
        TrdEnv,
        TrdMarket,
    )
except ImportError:  # pragma: no cover — only hit when futu-api not installed
    RET_OK = None  # type: ignore[assignment]
    OpenSecTradeContext = None  # type: ignore[assignment]
    SecurityFirm = None  # type: ignore[assignment]
    TrdEnv = None  # type: ignore[assignment]
    TrdMarket = None  # type: ignore[assignment]

logger = logging.getLogger("xenon.futu")


def _enum_to_str(value: Any) -> str:
    """Map a futu TrdEnv enum value back to its name ('REAL'/'SIMULATE').

    Returns the value's `.name` attribute when present (enum), otherwise the
    value itself when it's already a string. Raises on anything else so a
    silent mismatch can't propagate.
    """
    if value is None:
        raise ValueError("trd_env is None")
    name = getattr(value, "name", None)
    if name is not None:
        return str(name)
    if isinstance(value, str):
        return value
    raise ValueError(f"Unknown TrdEnv value: {value!r}")


# Defaults chosen to match Futu's 10 calls / 30s rate limit with safety margin.
DEFAULT_POSITION_TTL_SEC = 30
DEFAULT_ACCOUNT_TTL_SEC = 10
DEFAULT_COOLDOWN_SEC = 30


class FutuClient:
    """Read-only client for Futu OpenD positions and account info."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 11111,
        security_firm: str = "FUTUSECURITIES",
        trd_env: str = "REAL",
        filter_trading_market: str = "US",
        position_ttl_sec: int = DEFAULT_POSITION_TTL_SEC,
        account_ttl_sec: int = DEFAULT_ACCOUNT_TTL_SEC,
    ):
        self.host = host
        self.port = port
        self.security_firm = security_firm
        self.trd_env = trd_env
        self.filter_trading_market = filter_trading_market
        self._position_ttl_sec = position_ttl_sec
        self._account_ttl_sec = account_ttl_sec

        self._trd_ctx: Any = None
        self._acc_id: Optional[int] = None
        # Spec §10: ground-truth env of the actually-matched account row.
        # self.trd_env above is the *requested* env (logging only). This is
        # the *matched row's* env — the single source of truth for any
        # nav_history / scope persistence. None when not connected.
        self._matched_trd_env: Optional[str] = None
        self._connected = False

        self._lock = threading.RLock()

        self._positions_cache: Optional[Dict[str, Any]] = None
        self._positions_cache_time: Optional[datetime] = None
        self._positions_cooldown_until: Optional[datetime] = None

        self._account_cache: Optional[Dict[str, Any]] = None
        self._account_cache_time: Optional[datetime] = None
        self._account_cooldown_until: Optional[datetime] = None

    # ─────────────────────────────────────────────────────────────
    # Connection lifecycle
    # ─────────────────────────────────────────────────────────────

    def connect(self) -> None:
        """Open the OpenD trade context and select an account.

        Raises:
            FutuConnectionError: if OpenD is unreachable or returns no accounts.
            FutuAuthError: if OpenD rejects the connection.
        """
        if OpenSecTradeContext is None:
            raise FutuConnectionError("futu-api is not installed. `pip install futu-api`")

        trd_market = getattr(TrdMarket, self.filter_trading_market, TrdMarket.US)
        sec_firm = getattr(SecurityFirm, self.security_firm, SecurityFirm.FUTUSECURITIES)

        try:
            self._trd_ctx = OpenSecTradeContext(
                filter_trdmarket=trd_market,
                host=self.host,
                port=self.port,
                security_firm=sec_firm,
            )
            if self._trd_ctx is None:
                raise FutuConnectionError("OpenSecTradeContext returned None")

            ret, data = self._trd_ctx.get_acc_list()
            if ret != RET_OK:
                raise FutuConnectionError(f"get_acc_list failed: {data}")
            if data is None or data.empty:
                raise FutuConnectionError("No trading accounts returned from OpenD")

            env_enum = getattr(TrdEnv, self.trd_env, TrdEnv.REAL)
            matching = data[data["trd_env"] == env_enum]
            if matching.empty:
                # Spec §10: connect-time fallback — record the actual matched env
                # so callers don't get a silent lie (self.trd_env stays as the
                # *requested* value for logging).
                self._acc_id = int(data["acc_id"].iloc[0])
                self._matched_trd_env = _enum_to_str(data["trd_env"].iloc[0])
                logger.warning(
                    "No %s account on OpenD, falling back to first acc_id=%s env=%s",
                    self.trd_env,
                    self._acc_id,
                    self._matched_trd_env,
                )
            else:
                self._acc_id = int(matching["acc_id"].iloc[0])
                self._matched_trd_env = _enum_to_str(matching["trd_env"].iloc[0])

            self._connected = True
            logger.info(
                "FutuClient connected %s:%s acc_id=%s market=%s",
                self.host,
                self.port,
                self._acc_id,
                self.filter_trading_market,
            )
        except FutuError:
            raise
        except Exception as exc:
            raise classify_futu_exception(exc)

    def disconnect(self) -> None:
        if self._trd_ctx is not None:
            try:
                self._trd_ctx.close()
            except Exception as exc:  # best-effort
                logger.warning("FutuClient disconnect error: %s", exc)
            self._trd_ctx = None
        self._acc_id = None
        self._matched_trd_env = None
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected and self._trd_ctx is not None and self._acc_id is not None

    def trd_env_of_matched_account(self) -> Optional[str]:
        """Ground-truth env of the matched OpenD account row (spec §10).

        Returns 'REAL'/'SIMULATE' if connected, None otherwise. Always prefer
        this over `self.trd_env`, which is only the *requested* value and may
        not match after a connect-time fallback at line 184.
        """
        return self._matched_trd_env

    def _ensure_connected(self) -> None:
        if not self.is_connected():
            logger.info("FutuClient not connected — reconnecting")
            self._connected = False
            self._trd_ctx = None
            self.connect()

    # ─────────────────────────────────────────────────────────────
    # Positions
    # ─────────────────────────────────────────────────────────────

    def fetch_positions(self, force: bool = False) -> Dict[str, Any]:
        """Fetch positions, serving from TTL cache when fresh.

        Returns a dict shaped for direct JSON serialization:

            {
              "fetched_at": "2026-04-07T15:23:45.123Z",
              "data_as_of": "2026-04-07T15:23:45.123Z",
              "account_id": "12345",
              "source": "futu",
              "positions": [ {...}, ... ],
              "count": int,
              "is_stale": bool,
              "warnings": [str, ...]
            }
        """
        with self._lock:
            cached = self._serve_positions_cache(force=force)
            if cached is not None:
                return cached

            try:
                result = self._fetch_positions_impl()
            except FutuRateLimitError as exc:
                self._positions_cooldown_until = now_utc() + timedelta(seconds=exc.cooldown_seconds)
                if self._positions_cache is not None:
                    stale = dict(self._positions_cache)
                    stale["is_stale"] = True
                    stale["warnings"] = list(stale.get("warnings", [])) + [
                        f"rate-limited, served cache (cooldown {exc.cooldown_seconds}s)"
                    ]
                    return stale
                raise
            except FutuConnectionError:
                self._connected = False
                if self._positions_cache is not None:
                    stale = dict(self._positions_cache)
                    stale["is_stale"] = True
                    stale["warnings"] = list(stale.get("warnings", [])) + ["connection lost, served cache"]
                    return stale
                raise

            self._positions_cache = result
            self._positions_cache_time = now_utc()
            self._positions_cooldown_until = None
            return result

    def _serve_positions_cache(self, force: bool) -> Optional[Dict[str, Any]]:
        if force:
            return None
        now = now_utc()
        if self._positions_cooldown_until and now < self._positions_cooldown_until and self._positions_cache:
            stale = dict(self._positions_cache)
            stale["is_stale"] = True
            stale["warnings"] = list(stale.get("warnings", [])) + ["cooldown active"]
            return stale
        if (
            self._positions_cache is not None
            and self._positions_cache_time is not None
            and (now - self._positions_cache_time).total_seconds() < self._position_ttl_sec
        ):
            return self._positions_cache
        return None

    def _fetch_positions_impl(self) -> Dict[str, Any]:
        self._ensure_connected()
        try:
            from futu import RET_OK, TrdEnv
        except ImportError as exc:
            raise FutuConnectionError("futu-api missing") from exc

        env_enum = getattr(TrdEnv, self.trd_env, TrdEnv.REAL)

        try:
            ret, data = self._trd_ctx.position_list_query(
                trd_env=env_enum,
                acc_id=self._acc_id,
                refresh_cache=False,
            )
        except Exception as exc:
            raise classify_futu_exception(exc)

        if ret != RET_OK:
            # The Futu SDK returns error strings in `data` on failure.
            # Classify by message so the caller can distinguish rate
            # limit from real disconnects.
            raise classify_futu_exception(Exception(str(data)))

        positions: List[Dict[str, Any]] = []
        warnings: List[str] = []

        if data is None or data.empty:
            return self._positions_envelope(positions, warnings)

        for _, row in data.iterrows():
            try:
                pos = self._convert_position_row(row)
                if pos is not None:
                    positions.append(pos)
            except Exception as exc:  # never drop silently
                code = row.get("code", "unknown")
                logger.warning("Failed to convert futu position row %s: %s", code, exc)
                warnings.append(f"row {code}: {exc}")

        return self._positions_envelope(positions, warnings)

    def _positions_envelope(self, positions: List[Dict[str, Any]], warnings: List[str]) -> Dict[str, Any]:
        now_str = iso_z(now_utc())
        return {
            "fetched_at": now_str,
            "data_as_of": now_str,  # Futu position query is a snapshot; same value in v1
            "account_id": str(self._acc_id) if self._acc_id is not None else None,
            "source": "futu",
            "positions": positions,
            "count": len(positions),
            "is_stale": False,
            "warnings": warnings,
        }

    @staticmethod
    def _safe_float(value: Any, default: float = 0.0) -> float:
        # Futu returns the literal string "N/A" for fields it doesn't populate
        # for this account type (e.g. `unrealized_pl`, `realized_pl`,
        # `available_funds`). Coercing those to 0.0 silently was a real
        # correctness bug — callers get a zero that looks real.
        if value is None or value == "" or value == "N/A":
            return default
        try:
            f = float(value)
        except (TypeError, ValueError):
            return default
        # NaN check
        if f != f:
            return default
        return f

    @staticmethod
    def _maybe_float(value: Any) -> Optional[float]:
        """Like _safe_float but returns None instead of a default on missing.

        Use this for fields where "unknown" is semantically different from
        "zero" — e.g. the 'Dividends' card should render '—' not '$0.00'
        when Futu doesn't report it.
        """
        if value is None or value == "" or value == "N/A":
            return None
        try:
            f = float(value)
        except (TypeError, ValueError):
            return None
        if f != f:
            return None
        return f

    def _convert_position_row(self, row: Any) -> Optional[Dict[str, Any]]:
        code = row.get("code", "")
        qty = self._safe_float(row.get("qty", 0))
        if qty == 0:
            return None

        normalized = futu_to_ib(code)
        avg_cost = self._safe_float(
            row.get("cost_price"),
            default=self._safe_float(row.get("average_cost"), default=0.0),
        )
        nominal_price = self._safe_float(row.get("nominal_price"), default=0.0)
        market_value = self._safe_float(row.get("market_val"), default=nominal_price * qty)
        unrealized_pnl = self._safe_float(row.get("pl_val"), default=0.0)
        unrealized_pnl_pct = self._safe_float(row.get("pl_ratio"), default=0.0)
        currency = str(row.get("currency", "USD") or "USD").upper()

        return {
            "futu_code": code,
            "normalized": normalized,
            "quantity": qty,
            "avg_cost": avg_cost,
            "market_price": nominal_price,
            "market_value": market_value,
            "unrealized_pnl": unrealized_pnl,
            "unrealized_pnl_pct": unrealized_pnl_pct,
            "currency": currency,
            "position_side": str(row.get("position_side", "LONG")).upper(),
        }

    # ─────────────────────────────────────────────────────────────
    # Account info
    # ─────────────────────────────────────────────────────────────

    def fetch_account(self, force: bool = False) -> Dict[str, Any]:
        with self._lock:
            cached = self._serve_account_cache(force=force)
            if cached is not None:
                return cached

            try:
                result = self._fetch_account_impl()
            except FutuRateLimitError as exc:
                self._account_cooldown_until = now_utc() + timedelta(seconds=exc.cooldown_seconds)
                if self._account_cache is not None:
                    stale = dict(self._account_cache)
                    stale["is_stale"] = True
                    return stale
                raise
            except FutuConnectionError:
                self._connected = False
                if self._account_cache is not None:
                    stale = dict(self._account_cache)
                    stale["is_stale"] = True
                    return stale
                raise

            self._account_cache = result
            self._account_cache_time = now_utc()
            self._account_cooldown_until = None
            return result

    def _serve_account_cache(self, force: bool) -> Optional[Dict[str, Any]]:
        if force:
            return None
        now = now_utc()
        if self._account_cooldown_until and now < self._account_cooldown_until and self._account_cache:
            stale = dict(self._account_cache)
            stale["is_stale"] = True
            return stale
        if (
            self._account_cache is not None
            and self._account_cache_time is not None
            and (now - self._account_cache_time).total_seconds() < self._account_ttl_sec
        ):
            return self._account_cache
        return None

    def fetch_portfolio(self, force: bool = False) -> Dict[str, Any]:
        """Combined positions + account with computed aggregates.

        Returns an envelope that already has the derived fields the UI
        needs (total_unrealized_pnl, gross_position_value, excess_liquidity,
        etc.) so the TS adapter is a dumb lookup rather than reimplementing
        aggregation logic in JS.
        """
        positions_env = self.fetch_positions(force=force)
        account = self.fetch_account(force=force)

        positions: List[Dict[str, Any]] = positions_env.get("positions", [])

        total_unrealized = sum((p.get("unrealized_pnl") or 0.0) for p in positions)

        long_mv = 0.0
        short_mv = 0.0
        for p in positions:
            mv = p.get("market_value") or 0.0
            if mv >= 0:
                long_mv += mv
            else:
                short_mv += mv  # negative

        gross_position_value = long_mv + abs(short_mv)

        net_liquidation = account.get("net_liquidation") or 0.0
        maintenance_margin = account.get("maintenance_margin") or 0.0
        excess_liquidity = net_liquidation - maintenance_margin

        # Per the plan: these five have no honest Futu equivalent in v1.
        # They are set to None so the TS layer renders '—'.
        unmappable: Dict[str, None] = {
            "dividends": None,
            "previous_day_ewl": None,
            "reg_t_equity": None,
            "sma": None,
        }

        return {
            "fetched_at": positions_env.get("fetched_at"),
            "data_as_of": positions_env.get("data_as_of"),
            "account_id": positions_env.get("account_id"),
            "source": "futu",
            "is_stale": positions_env.get("is_stale", False) or account.get("is_stale", False),
            "warnings": positions_env.get("warnings", []),
            "positions": positions,
            "count": len(positions),
            # Aggregates the UI treats as AccountSummary fields.
            "account_summary": {
                "net_liquidation": net_liquidation,
                "equity_with_loan": net_liquidation,  # alias
                "cash": account.get("cash"),
                "settled_cash": account.get("cash"),  # Futu doesn't separate
                "buying_power": account.get("buying_power"),
                "available_funds": account.get("buying_power"),  # `power` is the equivalent
                "initial_margin": account.get("initial_margin"),
                "maintenance_margin": maintenance_margin,
                "excess_liquidity": excess_liquidity,
                "gross_position_value": gross_position_value,
                "unrealized_pnl": total_unrealized,
                # Per user decision: Day P&L shows the sum of positions' PnL.
                # (This is lifetime unrealized, not day-over-day — Futu doesn't
                # provide true daily deltas without snapshot infra.)
                "daily_pnl": total_unrealized,
                "realized_pnl": 0.0,
                **unmappable,
            },
            # Keep the raw account blob for debugging and future mapping.
            "account_raw": account.get("raw", {}),
        }

    def _fetch_account_impl(self) -> Dict[str, Any]:
        self._ensure_connected()
        try:
            from futu import RET_OK, Currency, TrdEnv
        except ImportError as exc:
            raise FutuConnectionError("futu-api missing") from exc

        env_enum = getattr(TrdEnv, self.trd_env, TrdEnv.REAL)

        try:
            ret, data = self._trd_ctx.accinfo_query(
                trd_env=env_enum,
                acc_id=self._acc_id,
                refresh_cache=False,
                currency=Currency.USD,
            )
        except Exception as exc:
            raise classify_futu_exception(exc)

        if ret != RET_OK:
            raise classify_futu_exception(Exception(str(data)))

        if data is None or data.empty:
            raise FutuDataError("accinfo_query returned empty DataFrame")

        row = data.iloc[0]
        get = self._safe_float
        maybe = self._maybe_float

        # Capture every column Futu returns so the TS adapter can map fields
        # we don't know about yet (day P&L, dividends, etc.). Values are
        # coerced to primitives for JSON safety.
        raw: Dict[str, Any] = {}
        for col in data.columns:
            val = row.get(col)
            if val is None:
                raw[col] = None
                continue
            try:
                if hasattr(val, "item"):
                    val = val.item()
            except Exception:
                pass
            if isinstance(val, float) and val != val:
                raw[col] = None
            elif isinstance(val, (int, float, str, bool)):
                raw[col] = val
            else:
                raw[col] = str(val)

        now_str = iso_z(now_utc())
        return {
            "fetched_at": now_str,
            "data_as_of": now_str,
            "account_id": str(self._acc_id) if self._acc_id is not None else None,
            "source": "futu",
            "raw": raw,
            "currency": "USD",
            "net_liquidation": maybe(row.get("total_assets")),
            "cash": maybe(row.get("cash")),
            "buying_power": maybe(row.get("power")),
            # Futu returns "N/A" for available_funds on this account; fall
            # back to `power` which is semantically equivalent.
            "available_funds": maybe(row.get("available_funds"))
            if row.get("available_funds") not in (None, "N/A", "")
            else maybe(row.get("power")),
            "maintenance_margin": maybe(row.get("maintenance_margin")),
            "initial_margin": maybe(row.get("initial_margin")),
            # These are "N/A" in the real dump; leave None rather than fake 0.
            "realized_pnl": maybe(row.get("realized_pl")),
            "unrealized_pnl": maybe(row.get("unrealized_pl")),
            "risk_level": maybe(row.get("risk_level")),
            "risk_status": str(row.get("risk_status")) if row.get("risk_status") not in (None, "N/A") else None,
            "long_mv": maybe(row.get("long_mv")),
            "short_mv": maybe(row.get("short_mv")),
            "is_stale": False,
        }
