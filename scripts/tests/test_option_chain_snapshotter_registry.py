"""Pre-work registry checks for option_chain_snapshotter.

Guards against ad-hoc clientId allocation drift (Pass-2 finding C-14 in the
design spec) and pins the LOCK_KEY value the single-instance guard relies on.
"""

from __future__ import annotations

from xenon.clients.ib_client import CLIENT_IDS


def test_option_chain_snapshotter_a_registered():
    assert CLIENT_IDS["option_chain_snapshotter_a"] == 901


def test_option_chain_snapshotter_b_registered():
    assert CLIENT_IDS["option_chain_snapshotter_b"] == 902


def test_snapshotter_ids_dont_collide_with_existing():
    """901 and 902 must not appear anywhere else in CLIENT_IDS."""
    duplicates = [
        name
        for name, cid in CLIENT_IDS.items()
        if cid in (901, 902) and name not in ("option_chain_snapshotter_a", "option_chain_snapshotter_b")
    ]
    assert duplicates == [], f"clientId collision: {duplicates}"


def test_lock_key_snapshotter_value():
    """7343001 is next in the xenon sequence after LOCK_KEY_VCG_CRI=7342001."""
    from xenon.api.services.advisory_lock import LOCK_KEY_OPTION_CHAIN_SNAPSHOTTER

    assert LOCK_KEY_OPTION_CHAIN_SNAPSHOTTER == 7343001


def test_lock_key_unique():
    """Single-instance guard depends on key uniqueness across xenon."""
    import xenon.api.services.advisory_lock as al

    keys = [v for k, v in vars(al).items() if k.startswith("LOCK_KEY_")]
    assert len(keys) == len(set(keys)), f"Duplicate LOCK_KEY values: {keys}"


def test_exchange_calendars_importable():
    """Per Pass-2 finding CL-2: exchange-calendars must be in pyproject.toml."""
    import exchange_calendars as ec

    nyse = ec.get_calendar("XNYS")
    assert nyse is not None
