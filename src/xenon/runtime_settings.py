"""Runtime settings: DB overrides for runtime knobs (one registry).

Each key has one `Setting` entry: env name, value type + validator, default,
category (toggle | tuning | safety), apply mode (live | restart), owner
process and consumers. Unknown keys and invalid values are refused.

Effective value (D3): a valid DB override (`xenon.runtime_settings`), else the
env/default value the consumer reads today. With no DB row every consumer
gets exactly its env value.

Rules per key (`rule`):

- `free`   — the DB value replaces the env value.
- `subset` — narrow-only set: the DB value must be a subset of the env value;
             effective = env ∩ DB (still narrow if env shrinks later).
- `min`    — narrow-only cap: DB <= env; effective = min(env, DB). An env
             value of None (invalid env = refuse all) stays None.
- `and`    — narrow-only flag: DB may turn it off; on only if env is on;
             effective = env AND DB.

Owners: `api` keys are resolved here. `relay` / `futu-quotes` keys run in a
process whose env the api cannot see: the api stores and serves the DB
override only, and that process combines it with its own env (T2).

Reads: one SELECT per `CACHE_TTL_S` per process. A committed write of this
process publishes its value into the cache at once (it is the fallback if
the next read fails) and makes any load started before it obsolete (a
generation counter). A DB read error keeps the last known overrides (none at
process start = env values). Writes of one key are serialized with a
transaction-scoped advisory lock, also when no row exists yet.
"""

from __future__ import annotations

import importlib
import json
import logging
import math
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger(__name__)

CACHE_TTL_S = 5.0

# Reason codes (top-level `reason_code` in API responses).
UNKNOWN_KEY = "SETTING_UNKNOWN"
INVALID_VALUE = "SETTING_INVALID_VALUE"
WIDENS_ENV = "SETTING_WIDENS_ENV"
CONFIRM_REQUIRED = "SETTING_CONFIRM_REQUIRED"


class SettingError(ValueError):
    """A refused value. `reason_code` is the API's top-level code."""

    def __init__(self, reason_code: str, detail: str):
        super().__init__(detail)
        self.reason_code = reason_code
        self.detail = detail


def _lazy(module: str, attr: str) -> Callable[..., Any]:
    """Resolve a consumer function at call time (no import cycle)."""

    def call(*args: Any) -> Any:
        return getattr(importlib.import_module(module), attr)(*args)

    return call


# ---------- validators: JSON value -> normalized value, or SettingError ----------


def _invalid(key: str, detail: str) -> SettingError:
    return SettingError(INVALID_VALUE, f"{key}: {detail}")


def _bool(key: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise _invalid(key, "must be true or false")
    return value


def _int_range(lo: int, hi: int) -> Callable[[str, Any], int]:
    def check(key: str, value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
            raise _invalid(key, f"must be an integer in [{lo}, {hi}]")
        return value

    return check


def _number_range(lo: float, hi: float, *, lo_open: bool = False) -> Callable[[str, Any], float | int]:
    def check(key: str, value: Any) -> float | int:
        ok = _finite_number(value)
        if ok:
            ok = (value > lo if lo_open else value >= lo) and value <= hi
        if not ok:
            raise _invalid(key, f"must be a number in {'(' if lo_open else '['}{lo:g}, {hi:g}]")
        return value

    return check


def _finite_number(value: Any) -> bool:
    """A JSON number that compares safely: ints compare exactly with any
    bound (no float conversion, so a huge int never overflows), floats
    must be finite."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return isinstance(value, int) or math.isfinite(value)


_GREEK_FACTOR_MAX = 1e6


def _enum(choices: tuple[str, ...]) -> Callable[[str, Any], str]:
    def check(key: str, value: Any) -> str:
        if value not in choices:
            raise _invalid(key, f"must be one of {list(choices)}")
        return value

    return check


def _set_of(norm: Callable[[str], str], allowed: Callable[[str], bool], what: str) -> Callable[[str, Any], list[str]]:
    def check(key: str, value: Any) -> list[str]:
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise _invalid(key, "must be a list of strings")
        items = sorted({norm(v) for v in value if v.strip()})
        bad = [v for v in items if not allowed(v)]
        if bad:
            raise _invalid(key, f"{bad} {'is' if len(bad) == 1 else 'are'} not {what}")
        return items

    return check


_GREEK_KEYS = ("iv", "delta", "gamma", "theta", "vega")


def _greek_factors(key: str, value: Any) -> dict[str, float]:
    if not isinstance(value, dict):
        raise _invalid(key, f"must be an object over {list(_GREEK_KEYS)}")
    out = {}
    for name, factor in value.items():
        if name not in _GREEK_KEYS:
            raise _invalid(key, f"{name!r} is not one of {list(_GREEK_KEYS)}")
        if not _finite_number(factor) or abs(factor) > _GREEK_FACTOR_MAX:
            raise _invalid(
                key, f"factor for {name!r} must be a number in [-{_GREEK_FACTOR_MAX:g}, {_GREEK_FACTOR_MAX:g}]"
            )
        out[name] = factor
    return out


def _is_tuple(entry: str) -> bool:
    return _lazy("xenon.execution.futu_orders", "_parse_tuple")(entry) is not None


def _is_order_type(label: str) -> bool:
    return label in importlib.import_module("xenon.execution.futu_orders").ORDER_TYPE_TO_FUTU


# ---------- registry ----------


@dataclass(frozen=True)
class Setting:
    key: str
    category: str  # toggle | tuning | safety
    value_type: str  # bool | int | number | enum | set | object (UI rendering hint)
    description: str
    validate: Callable[[str, Any], Any]
    owner: str = "api"  # api | relay | futu-quotes
    env: str | None = None  # env var name; None = DB-only key
    apply: str = "live"  # live | restart
    rule: str = "free"  # free | subset | min | and
    default: Any = None  # value when env is unset (owner's code default)
    env_value: Callable[[], Any] | None = None  # api-owned: env value as consumers read it today
    consumers: tuple[str, ...] = ()
    choices: tuple[str, ...] = ()
    bounds: tuple[float, float] | None = None
    confirm: Callable[[Any, Any], bool] | None = None  # (old_effective, new_effective) -> confirm needed


def _real_scope_changes(old: Any, new: Any) -> bool:
    return ("real" in (old or [])) != ("real" in (new or []))


def _any_change(old: Any, new: Any) -> bool:
    return old != new


def _turns_on(old: Any, new: Any) -> bool:
    return bool(new) and not bool(old)


_SAFETY_NOTE = "Narrow-only: the DB value may only restrict what the env allows."

_SETTINGS: tuple[Setting, ...] = (
    # Batch 1 — relay
    Setting(
        "XENON_QUOTES_DEFAULT",
        "toggle",
        "enum",
        "Default quote source for clients that do not choose one.",
        _enum(("ib", "futu-first")),
        owner="relay",
        env="XENON_QUOTES_DEFAULT",
        default="ib",
        choices=("ib", "futu-first"),
        consumers=("relay futu routing reconcile",),
    ),
    Setting(
        "XENON_FUTU_OPTIONS",
        "toggle",
        "bool",
        "Route option contract keys to Futu like stocks.",
        _bool,
        owner="relay",
        env="XENON_FUTU_OPTIONS",
        default=False,
        consumers=("relay futu routing reconcile",),
    ),
    Setting(
        "IB_REALTIME_MAX_LINES",
        "tuning",
        "int",
        "IB market-data line budget for the relay (account cap ~100, shared by every client).",
        _int_range(10, 100),
        owner="relay",
        env="IB_REALTIME_MAX_LINES",
        default=85,
        bounds=(10, 100),
        consumers=("relay line budget / eviction",),
    ),
    Setting(
        "IB_REALTIME_STALE_TICK_MS",
        "tuning",
        "int",
        "Tick age (ms) after which the relay marks a symbol stale.",
        _int_range(1_000, 3_600_000),
        owner="relay",
        env="IB_REALTIME_STALE_TICK_MS",
        default=60_000,
        bounds=(1_000, 3_600_000),
        consumers=("relay stale computation",),
    ),
    # Batch 1 — futu-quotes (forwarded by the relay)
    Setting(
        "FUTU_QUOTE_QUOTA",
        "tuning",
        "int",
        "Futu subscription quota ceiling (admission).",
        _int_range(1, 100_000),
        owner="futu-quotes",
        env="FUTU_QUOTE_QUOTA",
        default=800,
        bounds=(1, 100_000),
        consumers=("futu-quotes admission",),
    ),
    Setting(
        "FUTU_UNITS_PER_SYMBOL",
        "tuning",
        "int",
        "Quota units charged per held symbol.",
        _int_range(1, 10),
        owner="futu-quotes",
        env="FUTU_UNITS_PER_SYMBOL",
        default=2,
        bounds=(1, 10),
        consumers=("futu-quotes admission",),
    ),
    Setting(
        "FUTU_BOOK_STALE_S",
        "tuning",
        "number",
        "ORDER_BOOK receipt-age cutoff in seconds; 0 = off.",
        _number_range(0, 3600),
        owner="futu-quotes",
        env="FUTU_BOOK_STALE_S",
        default=0,
        bounds=(0, 3600),
        consumers=("futu-quotes book emit",),
    ),
    Setting(
        "FUTU_GREEK_FACTORS",
        "tuning",
        "object",
        "Per-field multipliers for option greeks/IV (iv usually 0.01). Unset = greeks/IV stay NULL.",
        _greek_factors,
        owner="futu-quotes",
        env="FUTU_GREEK_FACTORS",
        default=None,
        choices=_GREEK_KEYS,
        consumers=("futu-quotes option quote emit",),
    ),
    # Batch 2 — safety (api)
    Setting(
        "XENON_FUTU_TRADING",
        "safety",
        "set",
        "Futu trading scopes: simulate (paper), real (live). " + _SAFETY_NOTE,
        _set_of(lambda v: v.strip().lower(), lambda v: v in ("simulate", "real"), "simulate or real"),
        env="XENON_FUTU_TRADING",
        rule="subset",
        default=[],
        env_value=_lazy("xenon.execution.broker_capabilities", "env_trading_flags"),
        choices=("simulate", "real"),
        confirm=_real_scope_changes,
        consumers=("broker_capabilities.futu_trading_envs", "order gates", "/health futu"),
    ),
    Setting(
        "XENON_FUTU_TUPLES_LIVE",
        "safety",
        "set",
        "Allowed Futu live order tuples SEC:TYPE:TIF:SESSION. " + _SAFETY_NOTE,
        _set_of(lambda v: v.strip().upper(), _is_tuple, "SEC:TYPE:TIF:SESSION tuples"),
        env="XENON_FUTU_TUPLES_LIVE",
        rule="subset",
        env_value=lambda: _lazy("xenon.execution.futu_orders", "env_tuple_labels")("live"),
        confirm=_any_change,
        consumers=("futu_orders.allowed_tuples", "/health futu.capabilities"),
    ),
    Setting(
        "XENON_FUTU_TUPLES_PAPER",
        "safety",
        "set",
        "Allowed Futu paper order tuples SEC:TYPE:TIF:SESSION. " + _SAFETY_NOTE,
        _set_of(lambda v: v.strip().upper(), _is_tuple, "SEC:TYPE:TIF:SESSION tuples"),
        env="XENON_FUTU_TUPLES_PAPER",
        rule="subset",
        env_value=lambda: _lazy("xenon.execution.futu_orders", "env_tuple_labels")("paper"),
        consumers=("futu_orders.allowed_tuples", "/health futu.capabilities"),
    ),
    Setting(
        "XENON_FUTU_MODIFY_TYPES",
        "safety",
        "set",
        "Order types whose Futu modify is on. " + _SAFETY_NOTE,
        _set_of(lambda v: v.strip().upper(), _is_order_type, "Futu order types"),
        env="XENON_FUTU_MODIFY_TYPES",
        rule="subset",
        env_value=lambda: sorted(_lazy("xenon.execution.futu_orders", "env_modify_types")()),
        consumers=("futu_orders.modify_types",),
    ),
    Setting(
        "XENON_FUTU_MKT_NOTIONAL_CAP",
        "safety",
        "number",
        "Futu market-order notional cap (USD); effective = min(env, DB). " + _SAFETY_NOTE,
        _number_range(0, 1e12, lo_open=True),
        env="XENON_FUTU_MKT_NOTIONAL_CAP",
        rule="min",
        default=25000,
        env_value=_lazy("xenon.execution.quote_guard", "env_mkt_notional_cap_json"),
        consumers=("quote_guard.mkt_notional_cap",),
    ),
    Setting(
        "XENON_GATE4_NETTING",
        "safety",
        "bool",
        "Guaranteed-BAG netting in the coverage gate. " + _SAFETY_NOTE,
        _bool,
        env="XENON_GATE4_NETTING",
        rule="and",
        default=False,
        env_value=_lazy("xenon.execution.gate4", "env_netting_enabled"),
        consumers=("gate4.netting_enabled", "naked_short_audit"),
    ),
    Setting(
        "XENON_AUDIT_CANCEL",
        "safety",
        "bool",
        "Naked-short audit enforce mode (cancel violating orders). Enabling needs a typed confirm.",
        _bool,
        env="XENON_AUDIT_CANCEL",
        default=False,
        env_value=_lazy("xenon.api.services.audit_enforce", "env_enabled"),
        confirm=_turns_on,
        consumers=("audit_enforce.enabled",),
    ),
    Setting(
        "orders_paused",
        "safety",
        "bool",
        "Pause every place and modify (IB and Futu, all paths). Cancel stays allowed.",
        _bool,
        default=False,
        env_value=lambda: False,
        consumers=("guards.ensure_order_allowed",),
    ),
)

REGISTRY: dict[str, Setting] = {s.key: s for s in _SETTINGS}


# ---------- resolution ----------


def env_value(setting: Setting) -> Any:
    """The env value as the consumer reads it today; None for non-api owners."""
    if setting.owner != "api" or setting.env_value is None:
        return None
    return setting.env_value()


def combine(setting: Setting, env: Any, db: Any) -> Any:
    """Effective value from the env value and a validated DB value (None = no row)."""
    if db is None:
        return env
    if setting.owner != "api" or setting.rule == "free":
        return db
    if setting.rule == "subset":
        return sorted(set(env or []) & set(db))
    if setting.rule == "min":
        return None if env is None else min(env, db)
    if setting.rule == "and":
        return bool(env) and bool(db)
    raise AssertionError(setting.rule)  # pragma: no cover


def check_narrow(setting: Setting, env: Any, value: Any) -> None:
    """Refuse a DB value that would widen what the env allows."""
    if setting.owner != "api":
        return
    if setting.rule == "subset":
        extra = sorted(set(value) - set(env or []))
        if extra:
            raise SettingError(WIDENS_ENV, f"{setting.key}: {extra} not allowed by the env value {env or []}")
    elif setting.rule == "min":
        if env is None:
            raise SettingError(
                WIDENS_ENV, f"{setting.key}: the env value is invalid (all refused); no cap can widen it"
            )
        if value > env:
            raise SettingError(WIDENS_ENV, f"{setting.key}: {value:g} is above the env cap {env:g}")
    elif setting.rule == "and" and value and not env:
        raise SettingError(WIDENS_ENV, f"{setting.key}: the env value is off; the DB may only turn it off")


def validate(key: str, value: Any) -> tuple[Setting, Any]:
    setting = REGISTRY.get(key)
    if setting is None:
        raise SettingError(UNKNOWN_KEY, f"unknown setting {key!r}")
    return setting, setting.validate(key, value)


@dataclass
class _Cache:
    loaded_at: float = -math.inf
    overrides: dict[str, Any] = field(default_factory=dict)
    # Bumped by every committed write in this process. A load that started
    # under an older generation is obsolete and is never published.
    gen: int = 0


_cache = _Cache()
_lock = threading.Lock()
# Held by put/delete from before the transaction until after _publish_write, so
# this process publishes its writes in commit order (the advisory lock is gone
# at commit, before publication). An older write can then never replace a newer
# acknowledged one in the read-failure fallback.
# ponytail: one lock for all keys; settings writes are rare operator actions.
_write_lock = threading.Lock()


def invalidate() -> None:
    """Drop the cached overrides; the next read goes to the DB."""
    with _lock:
        _cache.loaded_at = -math.inf


def _publish_write(key: str, value: Any) -> None:
    """After a committed write: the new value (None = row deleted) becomes the
    known state at once — it is the fallback if the next DB read fails — and
    any load started before the write is made obsolete."""
    with _lock:
        _cache.gen += 1
        if value is None:
            _cache.overrides.pop(key, None)
        else:
            _cache.overrides[key] = value
        _cache.loaded_at = -math.inf


_env_only = False


def env_only() -> None:
    """This process never reads settings from the DB: every effective value is
    its env value (D5: the api is the only reader). For subprocesses such as
    `xenon-ib-sync`; the api passes them the effective values in their env."""
    global _env_only
    _env_only = True


def reset() -> None:
    """Tests: forget loaded overrides too (next read = DB or env)."""
    global _env_only
    with _lock:
        _cache.loaded_at = -math.inf
        _cache.overrides = {}
        _cache.gen += 1
        _env_only = False


def _load_rows() -> list[dict[str, Any]]:
    from sqlalchemy import select

    from xenon.db.engine import get_sync_engine
    from xenon.db.schema import runtime_settings

    with get_sync_engine().connect() as conn:
        return [dict(r._mapping) for r in conn.execute(select(runtime_settings))]


def overrides() -> dict[str, Any]:
    """Valid DB overrides by key (cached). Invalid or unknown rows are ignored
    (ERROR log): a bad row never changes behavior."""
    if _env_only:
        return {}
    with _lock:
        if time.monotonic() - _cache.loaded_at < CACHE_TTL_S:
            return dict(_cache.overrides)
        gen = _cache.gen
    try:
        rows = _load_rows()
    except Exception as exc:  # noqa: BLE001 — any DB failure keeps the last values
        log.warning("runtime_settings: DB read failed, keeping last values: %s", exc)
        with _lock:
            if _cache.gen == gen:
                _cache.loaded_at = time.monotonic()
            return dict(_cache.overrides)
    loaded: dict[str, Any] = {}
    for row in rows:
        try:
            _, value = validate(row["key"], row["value"])
        except Exception as exc:  # noqa: BLE001 — a bad row never breaks the read
            log.error("runtime_settings: ignoring DB row %r: %s", row["key"], getattr(exc, "detail", exc))
            continue
        loaded[row["key"]] = value
    with _lock:
        if _cache.gen != gen:
            # A write committed while this load ran: its snapshot is obsolete.
            return dict(_cache.overrides)
        _cache.overrides = loaded
        _cache.loaded_at = time.monotonic()
    return dict(loaded)


def override(key: str) -> Any:
    return overrides().get(key)


def apply(key: str, env: Any) -> Any:
    """Consumer entry point: effective value given the env value it parsed."""
    return combine(REGISTRY[key], env, override(key))


def effective(key: str) -> Any:
    setting = REGISTRY[key]
    return combine(setting, env_value(setting), override(key))


def orders_paused() -> bool:
    return bool(effective("orders_paused"))


# ---------- writes (API) ----------


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value))


# Advisory-lock namespace for runtime settings writes ("RST1").
_LOCK_NAMESPACE = 0x52535431


def _lock_key(conn: Any, key: str) -> None:
    """Serialize writers of one key for the rest of the transaction, also
    when no row exists yet (FOR UPDATE locks nothing on a missing row)."""
    from sqlalchemy import text

    conn.execute(text("SELECT pg_advisory_xact_lock(:ns, hashtext(:k))"), {"ns": _LOCK_NAMESPACE, "k": key})


def _check_confirm(setting: Setting, old_eff: Any, new_eff: Any, confirm: str | None) -> None:
    if setting.confirm is not None and setting.confirm(old_eff, new_eff) and confirm != setting.key:
        raise SettingError(CONFIRM_REQUIRED, f"{setting.key}: this change needs confirm={setting.key!r}")


def put(key: str, value: Any, *, actor: str | None, note: str | None, confirm: str | None) -> dict[str, Any]:
    """Validate, upsert the override and append an audit row (one transaction)."""
    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from xenon.db.engine import get_sync_engine
    from xenon.db.schema import runtime_settings, runtime_settings_audit

    setting, value = validate(key, value)
    env = env_value(setting)
    check_narrow(setting, env, value)
    with _write_lock:  # until published: see _write_lock
        with get_sync_engine().begin() as conn:
            _lock_key(conn, key)
            old = conn.execute(
                select(runtime_settings.c.value).where(runtime_settings.c.key == key).with_for_update()
            ).scalar_one_or_none()
            old_db = _valid_or_none(key, old)
            _check_confirm(setting, combine(setting, env, old_db), combine(setting, env, value), confirm)
            stored = _jsonable(value)
            stmt = pg_insert(runtime_settings).values(key=key, value=stored, updated_by=actor, note=note)
            conn.execute(
                stmt.on_conflict_do_update(
                    index_elements=["key"],
                    set_={"value": stmt.excluded.value, "updated_by": actor, "note": note, "updated_at": _now()},
                )
            )
            conn.execute(
                runtime_settings_audit.insert().values(
                    key=key, old_value=old, new_value=stored, changed_by=actor, note=note
                )
            )
        _publish_write(key, value)  # after commit, still under _write_lock
    return {"key": key, "changed": True}


def delete(key: str, *, actor: str | None, note: str | None, confirm: str | None) -> dict[str, Any]:
    """Remove the override (revert to env) and append an audit row."""
    from sqlalchemy import select

    from xenon.db.engine import get_sync_engine
    from xenon.db.schema import runtime_settings, runtime_settings_audit

    setting = REGISTRY.get(key)
    if setting is None:
        raise SettingError(UNKNOWN_KEY, f"unknown setting {key!r}")
    env = env_value(setting)
    with _write_lock:  # until published: see _write_lock
        with get_sync_engine().begin() as conn:
            _lock_key(conn, key)
            old = conn.execute(
                select(runtime_settings.c.value).where(runtime_settings.c.key == key).with_for_update()
            ).scalar_one_or_none()
            if old is None:
                return {"key": key, "changed": False}
            _check_confirm(setting, combine(setting, env, _valid_or_none(key, old)), env, confirm)
            conn.execute(runtime_settings.delete().where(runtime_settings.c.key == key))
            conn.execute(
                runtime_settings_audit.insert().values(
                    key=key, old_value=old, new_value=None, changed_by=actor, note=note
                )
            )
        _publish_write(key, None)  # after commit, still under _write_lock
    return {"key": key, "changed": True}


def _valid_or_none(key: str, raw: Any) -> Any:
    if raw is None:
        return None
    try:
        return validate(key, raw)[1]
    except Exception:  # noqa: BLE001 — invalid stored value = no override
        return None


def _now():
    from sqlalchemy import func

    return func.now()


def _iso(ts: Any) -> str | None:
    return ts.isoformat() if ts is not None else None


def describe() -> list[dict[str, Any]]:
    """GET /admin/settings payload: per key metadata, env/DB/effective values,
    and the last audit row. Reads the DB fresh (no cache)."""
    from sqlalchemy import func, select

    from xenon.db.engine import get_sync_engine
    from xenon.db.schema import runtime_settings, runtime_settings_audit

    audit = runtime_settings_audit
    latest = select(audit.c.key, func.max(audit.c.id).label("id")).group_by(audit.c.key).subquery()
    with get_sync_engine().connect() as conn:
        rows = {r.key: dict(r._mapping) for r in conn.execute(select(runtime_settings))}
        last = {r.key: dict(r._mapping) for r in conn.execute(select(audit).join(latest, audit.c.id == latest.c.id))}
    out = []
    for s in _SETTINGS:
        row = rows.get(s.key)
        db = _valid_or_none(s.key, row["value"]) if row else None
        env = env_value(s)
        change = last.get(s.key)
        out.append(
            {
                "key": s.key,
                "env_name": s.env,
                "owner": s.owner,
                "category": s.category,
                "type": s.value_type,
                "apply": s.apply,
                "rule": s.rule,
                "narrow_only": s.rule in ("subset", "min", "and"),
                "confirm_rule": None if s.confirm is None else s.confirm.__name__.lstrip("_"),
                "description": s.description,
                "consumers": list(s.consumers),
                "choices": list(s.choices),
                "bounds": list(s.bounds) if s.bounds else None,
                "default": s.default,
                "env_known": s.owner == "api",
                "env_value": env,
                "db_value": db,
                "db_value_invalid": bool(row) and db is None,
                "effective": combine(s, env, db) if s.owner == "api" else db,
                "updated_at": _iso(row["updated_at"]) if row else None,
                "updated_by": row["updated_by"] if row else None,
                "note": row["note"] if row else None,
                "last_change": None
                if change is None
                else {
                    "changed_at": _iso(change["changed_at"]),
                    "changed_by": change["changed_by"],
                    "old_value": change["old_value"],
                    "new_value": change["new_value"],
                    "note": change["note"],
                },
            }
        )
    return out
