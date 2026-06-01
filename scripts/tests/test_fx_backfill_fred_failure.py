"""FRED-unreachable backfill must abort loudly, not write NULL amounts."""

from datetime import date
from unittest.mock import patch

import httpx
import pytest

from xenon.utils.futu_fx import FxBackfillFailed, backfill_fx_series


def test_backfill_aborts_on_503_after_retries():
    response_503 = httpx.Response(503, request=httpx.Request("GET", "https://x"))

    with patch("xenon.utils.futu_fx._http_get") as mock_get:
        mock_get.return_value = response_503
        # Stub time.sleep so the retries don't wait the 7s backoff sum.
        with patch("xenon.utils.futu_fx.time.sleep"):
            with pytest.raises(FxBackfillFailed) as exc:
                backfill_fx_series("HKD", date(2024, 5, 1), date(2024, 5, 3))

    assert exc.value.reason_code == "fx_backfill_failed"
    # Retries exactly 3× per spec
    assert mock_get.call_count == 3


def test_backfill_writes_rows_on_success(seed_fx):
    csv_body = "observation_date,DEXHKUS\n2024-05-01,7.8234\n2024-05-02,7.8245\n2024-05-03,7.8201\n"
    fake = httpx.Response(
        200,
        content=csv_body.encode(),
        request=httpx.Request("GET", "https://x"),
    )
    with patch("xenon.utils.futu_fx._http_get", return_value=fake):
        rows_written = backfill_fx_series("HKD", date(2024, 5, 1), date(2024, 5, 3))
    assert rows_written == 3

    # Re-register the inserted rows with seed_fx so it cleans them up.
    for d_iso in ("2024-05-01", "2024-05-02", "2024-05-03"):
        # seed_fx is a callable that also overwrites; safe to re-insert
        # then have it tear them down.
        seed_fx("HKD", date.fromisoformat(d_iso), 7.8)
