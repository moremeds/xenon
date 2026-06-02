"""Guard for the futu SDK's eager log-dir creation under pytest-xdist.

futu/common/ft_logger.py calls os.makedirs() at module-import time without
exist_ok=True. Under -n 4+ workers, the second worker to import the SDK
fails on FileExistsError. conftest.py monkey-patches os.makedirs to always
pass exist_ok=True before any test runs; this guard ensures the patch is
still in place.

If this test fails, release.yml's `verify` job will likely fail with the
same race we saw on the v0.1.0 release attempt.
"""

from __future__ import annotations

import os
import tempfile


def test_os_makedirs_is_patched_to_be_idempotent():
    """A repeat os.makedirs() of the same path must NOT raise FileExistsError."""
    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, "futu_log_simulation")
        os.makedirs(target)  # first call creates
        # Second call with no exist_ok kwarg — patch should add it
        os.makedirs(target)
