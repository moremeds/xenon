"""regime_state view: deterministic latest-row selection via id DESC tiebreaker

Revision ID: a1b2c3d4e5f6
Revises: 48343156f9b7
Create Date: 2026-04-30 17:30:00.000000

Codex-review CODEX-7: the original view ordered only by `scanned_at DESC`
(VCG) / `recorded_at DESC` (CRI), so two inserts in the same `now()`
microsecond made `LIMIT 1` arbitrary — the view could surface either
row from the tied pair. Add `id DESC` as the tiebreaker so the latest
auto-incremented row wins on ties. Pure CREATE OR REPLACE; no data move.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "48343156f9b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_VIEW_DDL_WITH_TIEBREAKER = """
CREATE OR REPLACE VIEW xenon.regime_state AS
WITH latest_vcg AS (
    SELECT
        scanned_at,
        tier            AS vcg_tier_raw,
        regime          AS vcg_regime,
        ro,
        edr,
        bounce,
        sign_ok,
        sign_suppressed,
        pi_panic,
        vix
    FROM xenon.vcg_series
    ORDER BY scanned_at DESC, id DESC
    LIMIT 1
),
latest_cri AS (
    SELECT
        recorded_at,
        cri_level       AS cri_score,
        crash_trigger_fired,
        cta_forced_reduction,
        vix             AS cri_vix
    FROM xenon.cri_series
    ORDER BY recorded_at DESC, id DESC
    LIMIT 1
)
SELECT
    v.scanned_at        AS vcg_scanned_at,
    v.vcg_tier_raw,
    v.vcg_regime,
    v.ro                AS vcg_ro,
    v.edr               AS vcg_edr,
    v.bounce            AS vcg_bounce,
    v.sign_ok           AS vcg_sign_ok,
    v.sign_suppressed   AS vcg_sign_suppressed,
    v.pi_panic          AS vcg_pi_panic,
    v.vix               AS vcg_vix,
    c.recorded_at       AS cri_scanned_at,
    c.cri_score,
    c.crash_trigger_fired,
    c.cta_forced_reduction,
    c.cri_vix
FROM latest_vcg v CROSS JOIN latest_cri c
"""

_VIEW_DDL_ORIGINAL = """
CREATE OR REPLACE VIEW xenon.regime_state AS
WITH latest_vcg AS (
    SELECT
        scanned_at,
        tier            AS vcg_tier_raw,
        regime          AS vcg_regime,
        ro,
        edr,
        bounce,
        sign_ok,
        sign_suppressed,
        pi_panic,
        vix
    FROM xenon.vcg_series
    ORDER BY scanned_at DESC
    LIMIT 1
),
latest_cri AS (
    SELECT
        recorded_at,
        cri_level       AS cri_score,
        crash_trigger_fired,
        cta_forced_reduction,
        vix             AS cri_vix
    FROM xenon.cri_series
    ORDER BY recorded_at DESC
    LIMIT 1
)
SELECT
    v.scanned_at        AS vcg_scanned_at,
    v.vcg_tier_raw,
    v.vcg_regime,
    v.ro                AS vcg_ro,
    v.edr               AS vcg_edr,
    v.bounce            AS vcg_bounce,
    v.sign_ok           AS vcg_sign_ok,
    v.sign_suppressed   AS vcg_sign_suppressed,
    v.pi_panic          AS vcg_pi_panic,
    v.vix               AS vcg_vix,
    c.recorded_at       AS cri_scanned_at,
    c.cri_score,
    c.crash_trigger_fired,
    c.cta_forced_reduction,
    c.cri_vix
FROM latest_vcg v CROSS JOIN latest_cri c
"""


def upgrade() -> None:
    op.execute(_VIEW_DDL_WITH_TIEBREAKER)


def downgrade() -> None:
    op.execute(_VIEW_DDL_ORIGINAL)
