"""Anomaly rows can be written with NULL reported/reconstructed/delta and
several anomalies on the same date coexist.
"""

from datetime import date

from sqlalchemy import text

from xenon.db.engine import get_sync_engine
from xenon.db.queries.futu_history import write_recon_anomaly
from xenon.execution.account_scope import AccountScope

SCOPE = AccountScope("FUTU", "paper", "pytest")


def test_multiple_anomalies_same_date(clean_scope):
    write_recon_anomaly(
        SCOPE,
        date(2024, 5, 1),
        notes={"anomaly_type": "missing_fx"},
    )
    write_recon_anomaly(
        SCOPE,
        date(2024, 5, 1),
        notes={"anomaly_type": "over_close"},
    )
    with get_sync_engine().connect() as conn:
        n = conn.execute(
            text(
                """
                SELECT count(*) FROM xenon.futu_reconciliation_log
                 WHERE kind='anomaly' AND recon_date=:d
                   AND broker='FUTU' AND account_env='paper'
                   AND broker_account='pytest'
                """
            ),
            {"d": date(2024, 5, 1)},
        ).scalar()
    assert n == 2
