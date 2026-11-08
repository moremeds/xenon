"""Risk guard settings: effective values from ``xenon.runtime_settings``."""

from __future__ import annotations

import os
from dataclasses import fields, replace

from xenon import runtime_settings
from xenon.api.services.risk_guard.incidents import Thresholds

# field -> (lo, hi, list length or 0). Returns are fractions (-0.0075 = -0.75%).
THRESHOLD_BOUNDS: dict[str, tuple[float, float, int]] = {
    "watch_15m": (-0.5, 0.0, 0),
    "watch_day": (-0.5, 0.0, 0),
    "watch_cushion": (0.0, 1.0, 0),
    "fast_30m": (-0.5, 0.0, 0),
    "fast_day": (-0.5, 0.0, 0),
    "fast_strike_distance": (0.0, 0.5, 0),
    "fast_cushion": (0.0, 1.0, 0),
    "crash_day": (-0.5, 0.0, 0),
    "crash_60m": (-0.5, 0.0, 0),
    "crash_cushion": (0.0, 1.0, 0),
    "leverage_levels": (0.0, 100.0, 3),
    "leverage_step": (0.01, 100.0, 0),
    "drawdown_levels": (-1.0, 0.0, 3),
    "drawdown_step": (0.001, 1.0, 0),
    "repeat_s": (10.0, 3600.0, 0),
    "clear_s": (30.0, 86400.0, 0),
    "worsen_low": (0.0005, 0.5, 0),
    "worsen_cushion": (0.001, 1.0, 0),
}
assert set(THRESHOLD_BOUNDS) == {f.name for f in fields(Thresholds)}


def env_enabled() -> bool:
    return os.environ.get("XENON_RISK_GUARD", "").strip().lower() in {"1", "true", "yes", "on"}


def enabled() -> bool:
    return bool(runtime_settings.effective("XENON_RISK_GUARD"))


def thresholds() -> Thresholds:
    over = runtime_settings.effective("XENON_RISK_THRESHOLDS") or {}
    return replace(Thresholds(), **{k: tuple(v) if isinstance(v, list) else v for k, v in over.items()})


def key_levels() -> tuple[float, ...]:
    return tuple(runtime_settings.effective("XENON_RISK_KEY_LEVELS") or ())


def watch_symbols() -> tuple[str, ...]:
    return tuple(runtime_settings.effective("XENON_RISK_WATCH_SYMBOLS") or ())
