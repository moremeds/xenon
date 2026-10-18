"""Probe session for `scripts/tests/test_test_db_guard.py` under the api
conftest (not collected by default). Same probes as
`scripts/tests/db_guard_probe.py`; collected here so the api autouse
fixture is the one under test."""

from scripts.tests.db_guard_probe import (  # noqa: F401
    test_probe_cached_engine_never_survives_into_test,
    test_probe_import_time_singleton_bound_to_test_db,
)
