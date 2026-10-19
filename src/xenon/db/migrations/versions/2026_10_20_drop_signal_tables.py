"""drop the dead signal-layer tables (pure-portfolio pivot leftovers)

Revision ID: 2026_10_20_drop_signal_tables
Revises: 2026_10_18_order_state_check
Create Date: 2026-10-20

DATA-5 / DATA-16. The scanner / UW-analyze / flow-tracker layer was removed in
the pure-portfolio pivot and its query modules are gone. Nothing in src/,
scripts/, web/, deploy/ or docker/ reads or writes these tables any more:

  scan_results, cri_series, vcg_series, gex_snapshots,
  uw_analyze_snapshots, uw_analyze_flow_alerts, uw_analyze_gex_strikes,
  uw_analyze_short_volume_trend, uw_flow_events, uw_flow_event_ticks,
  ticker_cache, uw_api_stats

Dependents dropped with them:
  * view xenon.regime_state (projects the latest vcg_series + cri_series row;
    no reader outside the dropped tables)
  * the four fan-out triggers and functions installed by
    008128225148_normalize_payloads (fanout_uw_analyze_flow_alerts,
    fanout_uw_analyze_gex_strikes, fanout_uw_analyze_short_volume_trend,
    fanout_uw_flow_event_ticks)
  * indexes, FKs and owned sequences (go with their table)

NOT touched: xenon.regime_overrides (live order-path audit table). Its FK
targets order_submissions, not the dropped tables.

The drop is plain DROP (no CASCADE): an object this revision does not know
about that depends on a dropped table makes the migration fail and roll back
(alembic runs it in one transaction) instead of being silently removed.

DESTRUCTIVE. Operator: pg_dump exactly these tables BEFORE running the
migrator (see CHANGELOG ### Database).

downgrade() recreates the structure only (tables, generated columns, indexes,
FKs, sequences, triggers, functions, regime_state view), copied from a
database built by the old revisions. Rows are not restored.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2026_10_20_drop_signal_tables"
down_revision: Union[str, Sequence[str], None] = "2026_10_18_order_state_check"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Children before parents (FK order). A test pins this list against
# xenon.db.schema.metadata.
DROPPED_TABLES: tuple[str, ...] = (
    "uw_analyze_flow_alerts",
    "uw_analyze_gex_strikes",
    "uw_analyze_short_volume_trend",
    "uw_flow_event_ticks",
    "uw_analyze_snapshots",
    "uw_flow_events",
    "scan_results",
    "cri_series",
    "vcg_series",
    "gex_snapshots",
    "ticker_cache",
    "uw_api_stats",
)

DROPPED_VIEWS: tuple[str, ...] = ("regime_state",)

DROPPED_FUNCTIONS: tuple[str, ...] = (
    "fanout_uw_analyze_flow_alerts",
    "fanout_uw_analyze_gex_strikes",
    "fanout_uw_analyze_short_volume_trend",
    "fanout_uw_flow_event_ticks",
)

_REGIME_STATE_VIEW = """
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

_FANOUT_FUNCTIONS: tuple[str, ...] = (
    """
CREATE OR REPLACE FUNCTION xenon.fanout_uw_analyze_flow_alerts()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
    DECLARE
      alert jsonb;
      pos int := 0;
    BEGIN
      IF NEW.flow_alerts IS NULL OR jsonb_typeof(NEW.flow_alerts) <> 'array' THEN
        RETURN NEW;
      END IF;
      FOR alert IN SELECT * FROM jsonb_array_elements(NEW.flow_alerts) LOOP
        INSERT INTO xenon.uw_analyze_flow_alerts (
          snapshot_id, ordinal, ticker, snapshot_at, alert_type, alert_severity, alert_payload
        ) VALUES (
          NEW.id, pos, NEW.ticker, NEW.snapshot_at,
          alert->>'type', alert->>'severity', alert
        )
        ON CONFLICT (snapshot_id, ordinal) DO UPDATE SET
          ticker = EXCLUDED.ticker,
          snapshot_at = EXCLUDED.snapshot_at,
          alert_type = EXCLUDED.alert_type,
          alert_severity = EXCLUDED.alert_severity,
          alert_payload = EXCLUDED.alert_payload;
        pos := pos + 1;
      END LOOP;
      DELETE FROM xenon.uw_analyze_flow_alerts
        WHERE snapshot_id = NEW.id AND ordinal >= pos;
      RETURN NEW;
    END $function$
    """,
    """
CREATE OR REPLACE FUNCTION xenon.fanout_uw_analyze_gex_strikes()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
    DECLARE strike_row jsonb; seen_strikes numeric[] := ARRAY[]::numeric[]; s numeric;
    BEGIN
      IF NEW.display IS NULL OR (NEW.display->'gex_by_strike') IS NULL THEN
        RETURN NEW;
      END IF;
      IF jsonb_typeof(NEW.display->'gex_by_strike') <> 'array' THEN
        RETURN NEW;
      END IF;
      FOR strike_row IN SELECT * FROM jsonb_array_elements(NEW.display->'gex_by_strike') LOOP
        s := (strike_row->>'strike')::numeric;
        seen_strikes := array_append(seen_strikes, s);
        INSERT INTO xenon.uw_analyze_gex_strikes (
          snapshot_id, ticker, snapshot_at, strike,
          call_gamma, put_gamma, net_gamma, distance_pct,
          is_call_wall, is_put_wall
        ) VALUES (
          NEW.id, NEW.ticker, NEW.snapshot_at, s,
          (strike_row->>'call_gamma')::numeric,
          (strike_row->>'put_gamma')::numeric,
          (strike_row->>'net_gamma')::numeric,
          (strike_row->>'distance_pct')::numeric,
          (strike_row->>'is_call_wall')::boolean,
          (strike_row->>'is_put_wall')::boolean
        )
        ON CONFLICT (snapshot_id, strike) DO UPDATE SET
          call_gamma = EXCLUDED.call_gamma,
          put_gamma = EXCLUDED.put_gamma,
          net_gamma = EXCLUDED.net_gamma,
          distance_pct = EXCLUDED.distance_pct,
          is_call_wall = EXCLUDED.is_call_wall,
          is_put_wall = EXCLUDED.is_put_wall;
      END LOOP;
      DELETE FROM xenon.uw_analyze_gex_strikes
        WHERE snapshot_id = NEW.id AND NOT (strike = ANY(seen_strikes));
      RETURN NEW;
    END $function$
    """,
    """
CREATE OR REPLACE FUNCTION xenon.fanout_uw_analyze_short_volume_trend()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
    DECLARE
      ratio_val jsonb;
      pos int := 0;
    BEGIN
      IF NEW.display IS NULL OR (NEW.display->'short_volume_trend') IS NULL THEN
        RETURN NEW;
      END IF;
      IF jsonb_typeof(NEW.display->'short_volume_trend') <> 'array' THEN
        RETURN NEW;
      END IF;
      FOR ratio_val IN SELECT * FROM jsonb_array_elements(NEW.display->'short_volume_trend') LOOP
        INSERT INTO xenon.uw_analyze_short_volume_trend (
          snapshot_id, ticker, snapshot_at, position_in_trend, ratio
        ) VALUES (
          NEW.id, NEW.ticker, NEW.snapshot_at, pos, (ratio_val#>>'{}')::numeric
        )
        ON CONFLICT (snapshot_id, position_in_trend) DO UPDATE SET
          ticker = EXCLUDED.ticker,
          snapshot_at = EXCLUDED.snapshot_at,
          ratio = EXCLUDED.ratio;
        pos := pos + 1;
      END LOOP;
      DELETE FROM xenon.uw_analyze_short_volume_trend
        WHERE snapshot_id = NEW.id AND position_in_trend >= pos;
      RETURN NEW;
    END $function$
    """,
    """
CREATE OR REPLACE FUNCTION xenon.fanout_uw_flow_event_ticks()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
    DECLARE
      tick jsonb;
      observed timestamptz;
      track_d date;
      seen_ts timestamptz[] := ARRAY[]::timestamptz[];
    BEGIN
      IF NEW.daily_track IS NULL THEN
        RETURN NEW;
      END IF;
      IF jsonb_typeof(NEW.daily_track) = 'array' THEN
        FOR tick IN SELECT * FROM jsonb_array_elements(NEW.daily_track) LOOP
          track_d := (tick->>'date')::date;
          observed := (track_d::timestamp + INTERVAL '20 hours') AT TIME ZONE 'UTC';
          seen_ts := array_append(seen_ts, observed);
          INSERT INTO xenon.uw_flow_event_ticks (
            event_id, flow_event_key, observed_at, track_date,
            oi, volume, mid, underlying_price, pct_change_premium, tick_payload
          ) VALUES (
            NEW.id, NEW.flow_event_key, observed, track_d,
            (tick->>'oi')::int,
            (tick->>'volume')::int,
            (tick->>'mid')::numeric,
            (tick->>'underlying_price')::numeric,
            (tick->>'pct_change_premium')::numeric,
            tick
          )
          ON CONFLICT (event_id, observed_at) DO UPDATE SET
            track_date = EXCLUDED.track_date,
            oi = EXCLUDED.oi,
            volume = EXCLUDED.volume,
            mid = EXCLUDED.mid,
            underlying_price = EXCLUDED.underlying_price,
            pct_change_premium = EXCLUDED.pct_change_premium,
            tick_payload = EXCLUDED.tick_payload;
        END LOOP;
      ELSIF jsonb_typeof(NEW.daily_track) = 'object' THEN
        FOR observed, tick IN
          SELECT key::timestamptz, value FROM jsonb_each(NEW.daily_track)
        LOOP
          seen_ts := array_append(seen_ts, observed);
          INSERT INTO xenon.uw_flow_event_ticks (
            event_id, flow_event_key, observed_at, track_date,
            oi, volume, mid, underlying_price, pct_change_premium, tick_payload
          ) VALUES (
            NEW.id, NEW.flow_event_key, observed, observed::date,
            (tick->>'oi')::int,
            (tick->>'volume')::int,
            (tick->>'mid')::numeric,
            (tick->>'underlying_price')::numeric,
            (tick->>'pct_change_premium')::numeric,
            tick
          )
          ON CONFLICT (event_id, observed_at) DO UPDATE SET
            track_date = EXCLUDED.track_date,
            oi = EXCLUDED.oi,
            volume = EXCLUDED.volume,
            mid = EXCLUDED.mid,
            underlying_price = EXCLUDED.underlying_price,
            pct_change_premium = EXCLUDED.pct_change_premium,
            tick_payload = EXCLUDED.tick_payload;
        END LOOP;
      END IF;
      DELETE FROM xenon.uw_flow_event_ticks
        WHERE event_id = NEW.id
          AND array_length(seen_ts, 1) IS NOT NULL
          AND NOT (observed_at = ANY(seen_ts));
      RETURN NEW;
    END $function$
    """,
)

# Structure of the dropped tables as built by the old revisions: tables,
# generated columns, sequences, defaults, PKs, uniques, indexes, triggers, FKs.
_TABLE_DDL: tuple[str, ...] = (
    """
CREATE TABLE xenon.cri_series (
    id bigint NOT NULL,
    cri_level numeric(8,4) NOT NULL,
    alert boolean DEFAULT false,
    payload jsonb,
    recorded_at timestamp with time zone DEFAULT now() NOT NULL,
    recorded_date date GENERATED ALWAYS AS (make_date((split_part((payload ->> 'date'::text), '-'::text, 1))::integer, (split_part((payload ->> 'date'::text), '-'::text, 2))::integer, (split_part((payload ->> 'date'::text), '-'::text, 3))::integer)) STORED,
    vix numeric(8,4) GENERATED ALWAYS AS (((payload ->> 'vix'::text))::numeric) STORED,
    vvix numeric(8,4) GENERATED ALWAYS AS (((payload ->> 'vvix'::text))::numeric) STORED,
    spy numeric(10,4) GENERATED ALWAYS AS (((payload ->> 'spy'::text))::numeric) STORED,
    vix_5d_roc numeric(8,4) GENERATED ALWAYS AS (((payload ->> 'vix_5d_roc'::text))::numeric) STORED,
    vvix_vix_ratio numeric(8,4) GENERATED ALWAYS AS (((payload ->> 'vvix_vix_ratio'::text))::numeric) STORED,
    spx_100d_ma numeric(10,4) GENERATED ALWAYS AS (((payload ->> 'spx_100d_ma'::text))::numeric) STORED,
    spx_distance_pct numeric(8,4) GENERATED ALWAYS AS (((payload ->> 'spx_distance_pct'::text))::numeric) STORED,
    cor1m numeric(6,4) GENERATED ALWAYS AS (((payload ->> 'cor1m'::text))::numeric) STORED,
    cor1m_previous_close numeric(6,4) GENERATED ALWAYS AS (((payload ->> 'cor1m_previous_close'::text))::numeric) STORED,
    cor1m_5d_change numeric(6,4) GENERATED ALWAYS AS (((payload ->> 'cor1m_5d_change'::text))::numeric) STORED,
    realized_vol numeric(8,4) GENERATED ALWAYS AS (((payload ->> 'realized_vol'::text))::numeric) STORED,
    cri_score numeric(8,4) GENERATED ALWAYS AS ((((payload -> 'cri'::text) ->> 'score'::text))::numeric) STORED,
    cri_components jsonb GENERATED ALWAYS AS (((payload -> 'cri'::text) -> 'components'::text)) STORED,
    cta_exposure_pct numeric(6,2) GENERATED ALWAYS AS ((((payload -> 'cta'::text) ->> 'exposure_pct'::text))::numeric) STORED,
    cta_forced_reduction boolean GENERATED ALWAYS AS ((((payload -> 'cta'::text) ->> 'forced_reduction'::text))::boolean) STORED,
    cta_selling_usd_b numeric(8,2) GENERATED ALWAYS AS ((((payload -> 'cta'::text) ->> 'selling_usd_b'::text))::numeric) STORED,
    menthorq_cta_score numeric(8,4) GENERATED ALWAYS AS ((((payload -> 'menthorq_cta'::text) ->> 'score'::text))::numeric) STORED,
    crash_trigger_fired boolean GENERATED ALWAYS AS ((((payload -> 'crash_trigger'::text) ->> 'fired'::text))::boolean) STORED
)
    """,
    """
CREATE SEQUENCE xenon.cri_series_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
    """,
    """
ALTER SEQUENCE xenon.cri_series_id_seq OWNED BY xenon.cri_series.id
    """,
    """
CREATE TABLE xenon.gex_snapshots (
    id bigint NOT NULL,
    ticker text NOT NULL,
    data_date date,
    scanned_at timestamp with time zone DEFAULT now() NOT NULL,
    payload jsonb NOT NULL,
    spot numeric(12,4) GENERATED ALWAYS AS (((payload ->> 'spot'::text))::numeric) STORED,
    net_gex numeric(14,2) GENERATED ALWAYS AS (((payload ->> 'net_gex'::text))::numeric) STORED,
    net_dex numeric(14,2) GENERATED ALWAYS AS (((payload ->> 'net_dex'::text))::numeric) STORED,
    vol_pc numeric(8,4) GENERATED ALWAYS AS (((payload ->> 'vol_pc'::text))::numeric) STORED,
    iv_30d numeric(6,4) GENERATED ALWAYS AS ((((payload -> 'iv'::text) ->> 'iv30d'::text))::numeric) STORED,
    iv_rank numeric(6,2) GENERATED ALWAYS AS ((((payload -> 'iv'::text) ->> 'iv_rank'::text))::numeric) STORED,
    hv_30d numeric(6,4) GENERATED ALWAYS AS ((((payload -> 'iv'::text) ->> 'hv30'::text))::numeric) STORED,
    mq_iv_30d numeric(6,4) GENERATED ALWAYS AS ((((payload -> 'iv'::text) ->> 'mq_iv30d'::text))::numeric) STORED,
    level_max_magnet_strike numeric(12,4) GENERATED ALWAYS AS (((((payload -> 'levels'::text) -> 'max_magnet'::text) ->> 'strike'::text))::numeric) STORED,
    level_max_magnet_gamma numeric(14,4) GENERATED ALWAYS AS (((((payload -> 'levels'::text) -> 'max_magnet'::text) ->> 'gamma'::text))::numeric) STORED,
    level_second_magnet_strike numeric(12,4) GENERATED ALWAYS AS (((((payload -> 'levels'::text) -> 'second_magnet'::text) ->> 'strike'::text))::numeric) STORED,
    level_second_magnet_gamma numeric(14,4) GENERATED ALWAYS AS (((((payload -> 'levels'::text) -> 'second_magnet'::text) ->> 'gamma'::text))::numeric) STORED,
    level_max_accelerator_strike numeric(12,4) GENERATED ALWAYS AS (((((payload -> 'levels'::text) -> 'max_accelerator'::text) ->> 'strike'::text))::numeric) STORED,
    level_max_accelerator_gamma numeric(14,4) GENERATED ALWAYS AS (((((payload -> 'levels'::text) -> 'max_accelerator'::text) ->> 'gamma'::text))::numeric) STORED,
    level_put_wall_strike numeric(12,4) GENERATED ALWAYS AS (((((payload -> 'levels'::text) -> 'put_wall'::text) ->> 'strike'::text))::numeric) STORED,
    level_call_wall_strike numeric(12,4) GENERATED ALWAYS AS (((((payload -> 'levels'::text) -> 'call_wall'::text) ->> 'strike'::text))::numeric) STORED,
    level_gex_flip_strike numeric(12,4) GENERATED ALWAYS AS (((((payload -> 'levels'::text) -> 'gex_flip'::text) ->> 'strike'::text))::numeric) STORED
)
    """,
    """
CREATE SEQUENCE xenon.gex_snapshots_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
    """,
    """
ALTER SEQUENCE xenon.gex_snapshots_id_seq OWNED BY xenon.gex_snapshots.id
    """,
    """
CREATE TABLE xenon.vcg_series (
    id bigint NOT NULL,
    scanned_at timestamp with time zone DEFAULT now() NOT NULL,
    market_open boolean,
    credit_proxy text,
    payload jsonb NOT NULL,
    vcg numeric(10,6) GENERATED ALWAYS AS ((((payload -> 'signal'::text) ->> 'vcg'::text))::numeric) STORED,
    vcg_adj numeric(10,6) GENERATED ALWAYS AS ((((payload -> 'signal'::text) ->> 'vcg_adj'::text))::numeric) STORED,
    residual numeric(12,8) GENERATED ALWAYS AS ((((payload -> 'signal'::text) ->> 'residual'::text))::numeric) STORED,
    beta1_vvix numeric(12,8) GENERATED ALWAYS AS ((((payload -> 'signal'::text) ->> 'beta1_vvix'::text))::numeric) STORED,
    beta2_vix numeric(12,8) GENERATED ALWAYS AS ((((payload -> 'signal'::text) ->> 'beta2_vix'::text))::numeric) STORED,
    alpha numeric(12,8) GENERATED ALWAYS AS ((((payload -> 'signal'::text) ->> 'alpha'::text))::numeric) STORED,
    vix numeric(8,4) GENERATED ALWAYS AS ((((payload -> 'signal'::text) ->> 'vix'::text))::numeric) STORED,
    vvix numeric(8,4) GENERATED ALWAYS AS ((((payload -> 'signal'::text) ->> 'vvix'::text))::numeric) STORED,
    credit_price numeric(10,4) GENERATED ALWAYS AS ((((payload -> 'signal'::text) ->> 'credit_price'::text))::numeric) STORED,
    credit_5d_return_pct numeric(8,4) GENERATED ALWAYS AS ((((payload -> 'signal'::text) ->> 'credit_5d_return_pct'::text))::numeric) STORED,
    ro smallint GENERATED ALWAYS AS ((((payload -> 'signal'::text) ->> 'ro'::text))::integer) STORED,
    edr smallint GENERATED ALWAYS AS ((((payload -> 'signal'::text) ->> 'edr'::text))::integer) STORED,
    tier smallint GENERATED ALWAYS AS ((((payload -> 'signal'::text) ->> 'tier'::text))::integer) STORED,
    bounce smallint GENERATED ALWAYS AS ((((payload -> 'signal'::text) ->> 'bounce'::text))::integer) STORED,
    vvix_severity text GENERATED ALWAYS AS (((payload -> 'signal'::text) ->> 'vvix_severity'::text)) STORED,
    sign_ok boolean GENERATED ALWAYS AS ((((payload -> 'signal'::text) ->> 'sign_ok'::text))::boolean) STORED,
    sign_suppressed boolean GENERATED ALWAYS AS ((((payload -> 'signal'::text) ->> 'sign_suppressed'::text))::boolean) STORED,
    pi_panic numeric(8,4) GENERATED ALWAYS AS ((((payload -> 'signal'::text) ->> 'pi_panic'::text))::numeric) STORED,
    regime text GENERATED ALWAYS AS (((payload -> 'signal'::text) ->> 'regime'::text)) STORED,
    interpretation text GENERATED ALWAYS AS (((payload -> 'signal'::text) ->> 'interpretation'::text)) STORED,
    attr_vvix_pct numeric(6,2) GENERATED ALWAYS AS (((((payload -> 'signal'::text) -> 'attribution'::text) ->> 'vvix_pct'::text))::numeric) STORED,
    attr_vix_pct numeric(6,2) GENERATED ALWAYS AS (((((payload -> 'signal'::text) -> 'attribution'::text) ->> 'vix_pct'::text))::numeric) STORED,
    attr_vvix_component numeric(12,8) GENERATED ALWAYS AS (((((payload -> 'signal'::text) -> 'attribution'::text) ->> 'vvix_component'::text))::numeric) STORED,
    attr_vix_component numeric(12,8) GENERATED ALWAYS AS (((((payload -> 'signal'::text) -> 'attribution'::text) ->> 'vix_component'::text))::numeric) STORED,
    attr_model_implied numeric(12,8) GENERATED ALWAYS AS (((((payload -> 'signal'::text) -> 'attribution'::text) ->> 'model_implied'::text))::numeric) STORED
)
    """,
    """
CREATE TABLE xenon.scan_results (
    id bigint NOT NULL,
    scan_type text NOT NULL,
    payload jsonb NOT NULL,
    scanned_at timestamp with time zone DEFAULT now() NOT NULL
)
    """,
    """
CREATE SEQUENCE xenon.scan_results_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
    """,
    """
ALTER SEQUENCE xenon.scan_results_id_seq OWNED BY xenon.scan_results.id
    """,
    """
CREATE TABLE xenon.ticker_cache (
    ticker text NOT NULL,
    cache_type text NOT NULL,
    data jsonb NOT NULL,
    expires_at timestamp with time zone,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
)
    """,
    """
CREATE TABLE xenon.uw_analyze_flow_alerts (
    id bigint NOT NULL,
    snapshot_id bigint NOT NULL,
    ordinal integer NOT NULL,
    ticker text NOT NULL,
    snapshot_at timestamp with time zone NOT NULL,
    alert_type text,
    alert_severity text,
    alert_payload jsonb NOT NULL
)
    """,
    """
CREATE SEQUENCE xenon.uw_analyze_flow_alerts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
    """,
    """
ALTER SEQUENCE xenon.uw_analyze_flow_alerts_id_seq OWNED BY xenon.uw_analyze_flow_alerts.id
    """,
    """
CREATE TABLE xenon.uw_analyze_gex_strikes (
    id bigint NOT NULL,
    snapshot_id bigint NOT NULL,
    ticker text NOT NULL,
    snapshot_at timestamp with time zone NOT NULL,
    strike numeric(12,4) NOT NULL,
    call_gamma numeric(14,4),
    put_gamma numeric(14,4),
    net_gamma numeric(14,4),
    distance_pct numeric(10,6),
    is_call_wall boolean,
    is_put_wall boolean
)
    """,
    """
CREATE SEQUENCE xenon.uw_analyze_gex_strikes_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
    """,
    """
ALTER SEQUENCE xenon.uw_analyze_gex_strikes_id_seq OWNED BY xenon.uw_analyze_gex_strikes.id
    """,
    """
CREATE TABLE xenon.uw_analyze_short_volume_trend (
    id bigint NOT NULL,
    snapshot_id bigint NOT NULL,
    ticker text NOT NULL,
    snapshot_at timestamp with time zone NOT NULL,
    position_in_trend integer NOT NULL,
    ratio numeric(8,6)
)
    """,
    """
CREATE SEQUENCE xenon.uw_analyze_short_volume_trend_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
    """,
    """
ALTER SEQUENCE xenon.uw_analyze_short_volume_trend_id_seq OWNED BY xenon.uw_analyze_short_volume_trend.id
    """,
    """
CREATE TABLE xenon.uw_analyze_snapshots (
    id bigint NOT NULL,
    ticker text NOT NULL,
    portfolio_score numeric(6,2),
    snapshot_at timestamp with time zone DEFAULT now() NOT NULL,
    report jsonb,
    display jsonb,
    derived jsonb,
    dark_pool_summary jsonb,
    options_flow_summary jsonb,
    flow_alerts jsonb,
    materialized_changes jsonb,
    report_fetched_at timestamp with time zone,
    archived_at timestamp with time zone,
    price numeric(12,4) GENERATED ALWAYS AS (((report ->> 'price'::text))::numeric) STORED,
    composite_score numeric(6,2) GENERATED ALWAYS AS ((((report -> 'scores'::text) ->> 'composite'::text))::numeric) STORED,
    flow_score numeric(6,2) GENERATED ALWAYS AS ((((report -> 'scores'::text) ->> 'flow'::text))::numeric) STORED,
    volatility_score numeric(6,2) GENERATED ALWAYS AS ((((report -> 'scores'::text) ->> 'volatility'::text))::numeric) STORED,
    market_structure_score numeric(6,2) GENERATED ALWAYS AS ((((report -> 'scores'::text) ->> 'market_structure'::text))::numeric) STORED,
    positioning_score numeric(6,2) GENERATED ALWAYS AS ((((report -> 'scores'::text) ->> 'positioning'::text))::numeric) STORED,
    grade text GENERATED ALWAYS AS (((report -> 'scores'::text) ->> 'grade'::text)) STORED,
    bias text GENERATED ALWAYS AS (((report -> 'scores'::text) ->> 'bias'::text)) STORED,
    mode text GENERATED ALWAYS AS (((report -> 'scores'::text) ->> 'mode'::text)) STORED,
    reweighted boolean GENERATED ALWAYS AS ((((report -> 'scores'::text) ->> 'reweighted'::text))::boolean) STORED,
    vrp_raw numeric(8,4) GENERATED ALWAYS AS ((((report -> 'vrp'::text) ->> 'vrp_raw'::text))::numeric) STORED,
    vrp_zscore numeric(8,4) GENERATED ALWAYS AS ((((report -> 'vrp'::text) ->> 'vrp_zscore'::text))::numeric) STORED,
    iv_percentile numeric(6,2) GENERATED ALWAYS AS ((((report -> 'vrp'::text) ->> 'iv_percentile'::text))::numeric) STORED,
    ts_ratio numeric(8,4) GENERATED ALWAYS AS ((((report -> 'vrp'::text) ->> 'ts_ratio'::text))::numeric) STORED,
    ts_inverted boolean GENERATED ALWAYS AS ((((report -> 'vrp'::text) ->> 'ts_inverted'::text))::boolean) STORED,
    earnings_within_14d boolean GENERATED ALWAYS AS ((((report -> 'vrp'::text) ->> 'earnings_within_14d'::text))::boolean) STORED,
    regime_label text GENERATED ALWAYS AS (((report -> 'regime'::text) ->> 'regime'::text)) STORED,
    regime_reason text GENERATED ALWAYS AS (((report -> 'regime'::text) ->> 'reason'::text)) STORED,
    gex_sign text GENERATED ALWAYS AS (((report -> 'regime'::text) ->> 'gex_sign'::text)) STORED,
    gex_flip_relative text GENERATED ALWAYS AS (((report -> 'regime'::text) ->> 'gex_flip_relative'::text)) STORED,
    flip_distance_pct numeric(8,4) GENERATED ALWAYS AS ((((report -> 'regime'::text) ->> 'flip_distance_pct'::text))::numeric) STORED,
    iv numeric(8,4) GENERATED ALWAYS AS (((display ->> 'iv'::text))::numeric) STORED,
    rv numeric(8,4) GENERATED ALWAYS AS (((display ->> 'rv'::text))::numeric) STORED,
    iv_rank numeric(6,2) GENERATED ALWAYS AS (((display ->> 'iv_rank'::text))::numeric) STORED,
    call_wall_strike numeric(12,4) GENERATED ALWAYS AS (((display ->> 'call_wall_strike'::text))::numeric) STORED,
    put_wall_strike numeric(12,4) GENERATED ALWAYS AS (((display ->> 'put_wall_strike'::text))::numeric) STORED,
    gamma_per_1pct numeric(18,4) GENERATED ALWAYS AS (((display ->> 'gamma_per_1pct'::text))::numeric) STORED,
    net_call_premium numeric(18,2) GENERATED ALWAYS AS (((display ->> 'net_call_premium'::text))::numeric) STORED,
    net_put_premium numeric(18,2) GENERATED ALWAYS AS (((display ->> 'net_put_premium'::text))::numeric) STORED,
    short_volume_ratio numeric(6,4) GENERATED ALWAYS AS (((display ->> 'short_volume_ratio'::text))::numeric) STORED,
    term_structure_label text GENERATED ALWAYS AS ((display ->> 'term_structure_label'::text)) STORED,
    max_pain numeric(12,4) GENERATED ALWAYS AS (((display ->> 'max_pain'::text))::numeric) STORED,
    derived_gex_sign text GENERATED ALWAYS AS ((derived ->> 'gex_sign'::text)) STORED,
    derived_call_wall numeric(12,4) GENERATED ALWAYS AS (((derived ->> 'call_wall'::text))::numeric) STORED,
    derived_put_wall numeric(12,4) GENERATED ALWAYS AS (((derived ->> 'put_wall'::text))::numeric) STORED,
    derived_max_pain numeric(12,4) GENERATED ALWAYS AS (((derived ->> 'max_pain'::text))::numeric) STORED,
    derived_spot numeric(12,4) GENERATED ALWAYS AS (((derived ->> 'spot'::text))::numeric) STORED,
    dp_score numeric(8,4) GENERATED ALWAYS AS (((dark_pool_summary ->> 'score'::text))::numeric) STORED,
    dp_signal text GENERATED ALWAYS AS ((dark_pool_summary ->> 'signal'::text)) STORED,
    dp_direction text GENERATED ALWAYS AS ((dark_pool_summary ->> 'direction'::text)) STORED,
    dp_strength integer GENERATED ALWAYS AS ((((dark_pool_summary ->> 'strength'::text))::numeric)::integer) STORED,
    dp_buy_ratio numeric(6,4) GENERATED ALWAYS AS (((dark_pool_summary ->> 'buy_ratio'::text))::numeric) STORED,
    dp_options_conflict boolean GENERATED ALWAYS AS (((dark_pool_summary ->> 'options_conflict'::text))::boolean) STORED,
    dp_num_prints integer GENERATED ALWAYS AS ((((dark_pool_summary ->> 'num_prints'::text))::numeric)::integer) STORED,
    dp_sustained_days integer GENERATED ALWAYS AS ((((dark_pool_summary ->> 'sustained_days'::text))::numeric)::integer) STORED,
    of_total_alerts integer GENERATED ALWAYS AS ((((options_flow_summary ->> 'total_alerts'::text))::numeric)::integer) STORED,
    of_total_premium numeric(18,2) GENERATED ALWAYS AS (((options_flow_summary ->> 'total_premium'::text))::numeric) STORED,
    of_call_premium numeric(18,2) GENERATED ALWAYS AS (((options_flow_summary ->> 'call_premium'::text))::numeric) STORED,
    of_put_premium numeric(18,2) GENERATED ALWAYS AS (((options_flow_summary ->> 'put_premium'::text))::numeric) STORED,
    of_call_put_ratio numeric(8,4) GENERATED ALWAYS AS (((options_flow_summary ->> 'call_put_ratio'::text))::numeric) STORED,
    of_bias text GENERATED ALWAYS AS ((options_flow_summary ->> 'bias'::text)) STORED,
    spy_iv_rank numeric(6,2) GENERATED ALWAYS AS (((((report -> 'benchmark'::text) -> 'spy'::text) ->> 'iv_rank'::text))::numeric) STORED,
    spy_gex_regime text GENERATED ALWAYS AS ((((report -> 'benchmark'::text) -> 'spy'::text) ->> 'gex_regime'::text)) STORED,
    sector_etf_ticker text GENERATED ALWAYS AS ((((report -> 'benchmark'::text) -> 'sector_etf'::text) ->> 'ticker'::text)) STORED,
    sector_etf_iv_rank numeric(6,2) GENERATED ALWAYS AS (((((report -> 'benchmark'::text) -> 'sector_etf'::text) ->> 'iv_rank'::text))::numeric) STORED,
    sector_etf_gex_regime text GENERATED ALWAYS AS ((((report -> 'benchmark'::text) -> 'sector_etf'::text) ->> 'gex_regime'::text)) STORED,
    sources jsonb,
    oi_baseline jsonb,
    previous_snapshot jsonb
)
    """,
    """
CREATE TABLE xenon.uw_api_stats (
    id bigint NOT NULL,
    bucket_hour timestamp with time zone NOT NULL,
    requests integer DEFAULT 0,
    cache_hits integer DEFAULT 0,
    latency_sum numeric(10,2) DEFAULT 0,
    latency_count integer DEFAULT 0,
    status_2xx integer DEFAULT 0,
    status_4xx integer DEFAULT 0,
    status_5xx integer DEFAULT 0
)
    """,
    """
CREATE SEQUENCE xenon.uw_api_stats_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
    """,
    """
ALTER SEQUENCE xenon.uw_api_stats_id_seq OWNED BY xenon.uw_api_stats.id
    """,
    """
CREATE TABLE xenon.uw_flow_event_ticks (
    id bigint NOT NULL,
    event_id bigint NOT NULL,
    flow_event_key text NOT NULL,
    observed_at timestamp with time zone NOT NULL,
    track_date date,
    oi integer,
    volume integer,
    mid numeric(10,4),
    underlying_price numeric(12,4),
    pct_change_premium numeric(10,6),
    tick_payload jsonb NOT NULL
)
    """,
    """
CREATE SEQUENCE xenon.uw_flow_event_ticks_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
    """,
    """
ALTER SEQUENCE xenon.uw_flow_event_ticks_id_seq OWNED BY xenon.uw_flow_event_ticks.id
    """,
    """
CREATE TABLE xenon.uw_flow_events (
    id bigint NOT NULL,
    ticker text NOT NULL,
    side text,
    strike numeric(12,2),
    expiry date,
    detected_at timestamp with time zone NOT NULL,
    initial jsonb NOT NULL,
    daily_track jsonb,
    status text NOT NULL,
    anomaly_reason text,
    closed_at timestamp with time zone,
    flow_event_key text NOT NULL,
    initial_premium_usd numeric(14,2) GENERATED ALWAYS AS (((initial ->> 'premium_usd'::text))::numeric) STORED,
    initial_oi integer GENERATED ALWAYS AS ((((initial ->> 'oi'::text))::numeric)::integer) STORED,
    initial_volume integer GENERATED ALWAYS AS ((((initial ->> 'volume'::text))::numeric)::integer) STORED,
    initial_mid numeric(10,4) GENERATED ALWAYS AS (((initial ->> 'mid'::text))::numeric) STORED,
    initial_underlying_price numeric(12,4) GENERATED ALWAYS AS (((initial ->> 'underlying_price'::text))::numeric) STORED
)
    """,
    """
CREATE SEQUENCE xenon.uw_flow_events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
    """,
    """
ALTER SEQUENCE xenon.uw_flow_events_id_seq OWNED BY xenon.uw_flow_events.id
    """,
    """
CREATE SEQUENCE xenon.uw_snapshots_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
    """,
    """
ALTER SEQUENCE xenon.uw_snapshots_id_seq OWNED BY xenon.uw_analyze_snapshots.id
    """,
    """
CREATE SEQUENCE xenon.vcg_series_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
    """,
    """
ALTER SEQUENCE xenon.vcg_series_id_seq OWNED BY xenon.vcg_series.id
    """,
    """
ALTER TABLE ONLY xenon.cri_series ALTER COLUMN id SET DEFAULT nextval('xenon.cri_series_id_seq'::regclass)
    """,
    """
ALTER TABLE ONLY xenon.gex_snapshots ALTER COLUMN id SET DEFAULT nextval('xenon.gex_snapshots_id_seq'::regclass)
    """,
    """
ALTER TABLE ONLY xenon.scan_results ALTER COLUMN id SET DEFAULT nextval('xenon.scan_results_id_seq'::regclass)
    """,
    """
ALTER TABLE ONLY xenon.uw_analyze_flow_alerts ALTER COLUMN id SET DEFAULT nextval('xenon.uw_analyze_flow_alerts_id_seq'::regclass)
    """,
    """
ALTER TABLE ONLY xenon.uw_analyze_gex_strikes ALTER COLUMN id SET DEFAULT nextval('xenon.uw_analyze_gex_strikes_id_seq'::regclass)
    """,
    """
ALTER TABLE ONLY xenon.uw_analyze_short_volume_trend ALTER COLUMN id SET DEFAULT nextval('xenon.uw_analyze_short_volume_trend_id_seq'::regclass)
    """,
    """
ALTER TABLE ONLY xenon.uw_analyze_snapshots ALTER COLUMN id SET DEFAULT nextval('xenon.uw_snapshots_id_seq'::regclass)
    """,
    """
ALTER TABLE ONLY xenon.uw_api_stats ALTER COLUMN id SET DEFAULT nextval('xenon.uw_api_stats_id_seq'::regclass)
    """,
    """
ALTER TABLE ONLY xenon.uw_flow_event_ticks ALTER COLUMN id SET DEFAULT nextval('xenon.uw_flow_event_ticks_id_seq'::regclass)
    """,
    """
ALTER TABLE ONLY xenon.uw_flow_events ALTER COLUMN id SET DEFAULT nextval('xenon.uw_flow_events_id_seq'::regclass)
    """,
    """
ALTER TABLE ONLY xenon.vcg_series ALTER COLUMN id SET DEFAULT nextval('xenon.vcg_series_id_seq'::regclass)
    """,
    """
ALTER TABLE ONLY xenon.cri_series
    ADD CONSTRAINT cri_series_pkey PRIMARY KEY (id)
    """,
    """
ALTER TABLE ONLY xenon.gex_snapshots
    ADD CONSTRAINT gex_snapshots_pkey PRIMARY KEY (id)
    """,
    """
ALTER TABLE ONLY xenon.scan_results
    ADD CONSTRAINT scan_results_pkey PRIMARY KEY (id)
    """,
    """
ALTER TABLE ONLY xenon.ticker_cache
    ADD CONSTRAINT ticker_cache_pkey PRIMARY KEY (ticker, cache_type)
    """,
    """
ALTER TABLE ONLY xenon.uw_analyze_flow_alerts
    ADD CONSTRAINT uq_uw_flow_alerts_snapshot_ordinal UNIQUE (snapshot_id, ordinal)
    """,
    """
ALTER TABLE ONLY xenon.uw_flow_event_ticks
    ADD CONSTRAINT uq_uw_flow_event_ticks UNIQUE (event_id, observed_at)
    """,
    """
ALTER TABLE ONLY xenon.uw_analyze_gex_strikes
    ADD CONSTRAINT uq_uw_gex_strikes_snapshot_strike UNIQUE (snapshot_id, strike)
    """,
    """
ALTER TABLE ONLY xenon.uw_analyze_short_volume_trend
    ADD CONSTRAINT uq_uw_short_vol_snapshot_pos UNIQUE (snapshot_id, position_in_trend)
    """,
    """
ALTER TABLE ONLY xenon.vcg_series
    ADD CONSTRAINT uq_vcg_series_scanned_at UNIQUE (scanned_at)
    """,
    """
ALTER TABLE ONLY xenon.uw_analyze_flow_alerts
    ADD CONSTRAINT uw_analyze_flow_alerts_pkey PRIMARY KEY (id)
    """,
    """
ALTER TABLE ONLY xenon.uw_analyze_gex_strikes
    ADD CONSTRAINT uw_analyze_gex_strikes_pkey PRIMARY KEY (id)
    """,
    """
ALTER TABLE ONLY xenon.uw_analyze_short_volume_trend
    ADD CONSTRAINT uw_analyze_short_volume_trend_pkey PRIMARY KEY (id)
    """,
    """
ALTER TABLE ONLY xenon.uw_api_stats
    ADD CONSTRAINT uw_api_stats_bucket_hour_key UNIQUE (bucket_hour)
    """,
    """
ALTER TABLE ONLY xenon.uw_api_stats
    ADD CONSTRAINT uw_api_stats_pkey PRIMARY KEY (id)
    """,
    """
ALTER TABLE ONLY xenon.uw_flow_event_ticks
    ADD CONSTRAINT uw_flow_event_ticks_pkey PRIMARY KEY (id)
    """,
    """
ALTER TABLE ONLY xenon.uw_flow_events
    ADD CONSTRAINT uw_flow_events_flow_event_key_key UNIQUE (flow_event_key)
    """,
    """
ALTER TABLE ONLY xenon.uw_flow_events
    ADD CONSTRAINT uw_flow_events_pkey PRIMARY KEY (id)
    """,
    """
ALTER TABLE ONLY xenon.uw_analyze_snapshots
    ADD CONSTRAINT uw_snapshots_pkey PRIMARY KEY (id)
    """,
    """
ALTER TABLE ONLY xenon.vcg_series
    ADD CONSTRAINT vcg_series_pkey PRIMARY KEY (id)
    """,
    """
CREATE INDEX ix_cri_crash_trigger ON xenon.cri_series USING btree (crash_trigger_fired) WHERE crash_trigger_fired
    """,
    """
CREATE INDEX ix_cri_cta_forced ON xenon.cri_series USING btree (cta_forced_reduction) WHERE cta_forced_reduction
    """,
    """
CREATE INDEX ix_cri_recorded_date ON xenon.cri_series USING btree (recorded_date)
    """,
    """
CREATE INDEX ix_gex_data_date ON xenon.gex_snapshots USING btree (data_date)
    """,
    """
CREATE INDEX ix_gex_scanned_at ON xenon.gex_snapshots USING btree (scanned_at)
    """,
    """
CREATE INDEX ix_gex_ticker_time ON xenon.gex_snapshots USING btree (ticker, scanned_at)
    """,
    """
CREATE INDEX ix_uw_analyze_snap_ticker_time ON xenon.uw_analyze_snapshots USING btree (ticker, snapshot_at)
    """,
    """
CREATE INDEX ix_uw_analyze_snap_time_bias ON xenon.uw_analyze_snapshots USING btree (snapshot_at, bias)
    """,
    """
CREATE INDEX ix_uw_analyze_snap_time_gex ON xenon.uw_analyze_snapshots USING btree (snapshot_at, gex_sign)
    """,
    """
CREATE INDEX ix_uw_analyze_snap_time_regime ON xenon.uw_analyze_snapshots USING btree (snapshot_at, regime_label)
    """,
    """
CREATE INDEX ix_uw_flow_alerts_snapshot ON xenon.uw_analyze_flow_alerts USING btree (snapshot_id)
    """,
    """
CREATE INDEX ix_uw_flow_alerts_ticker_time ON xenon.uw_analyze_flow_alerts USING btree (ticker, snapshot_at)
    """,
    """
CREATE INDEX ix_uw_flow_event_ticks_event_time ON xenon.uw_flow_event_ticks USING btree (event_id, observed_at)
    """,
    """
CREATE INDEX ix_uw_flow_event_ticks_observed_at ON xenon.uw_flow_event_ticks USING btree (observed_at)
    """,
    """
CREATE INDEX ix_uw_gex_strikes_snapshot ON xenon.uw_analyze_gex_strikes USING btree (snapshot_id)
    """,
    """
CREATE INDEX ix_uw_gex_strikes_ticker_time ON xenon.uw_analyze_gex_strikes USING btree (ticker, snapshot_at)
    """,
    """
CREATE INDEX ix_uw_short_vol_snapshot ON xenon.uw_analyze_short_volume_trend USING btree (snapshot_id)
    """,
    """
CREATE INDEX ix_vcg_regime ON xenon.vcg_series USING btree (regime)
    """,
    """
CREATE INDEX ix_vcg_scanned_at ON xenon.vcg_series USING btree (scanned_at)
    """,
    """
CREATE INDEX ix_vcg_tier ON xenon.vcg_series USING btree (tier) WHERE (tier IS NOT NULL)
    """,
    """
CREATE TRIGGER trg_uw_analyze_flow_alerts_fanout AFTER INSERT OR UPDATE OF flow_alerts ON xenon.uw_analyze_snapshots FOR EACH ROW EXECUTE FUNCTION xenon.fanout_uw_analyze_flow_alerts()
    """,
    """
CREATE TRIGGER trg_uw_analyze_gex_strikes_fanout AFTER INSERT OR UPDATE OF display ON xenon.uw_analyze_snapshots FOR EACH ROW EXECUTE FUNCTION xenon.fanout_uw_analyze_gex_strikes()
    """,
    """
CREATE TRIGGER trg_uw_analyze_short_volume_trend_fanout AFTER INSERT OR UPDATE OF display ON xenon.uw_analyze_snapshots FOR EACH ROW EXECUTE FUNCTION xenon.fanout_uw_analyze_short_volume_trend()
    """,
    """
CREATE TRIGGER trg_uw_flow_event_ticks_fanout AFTER INSERT OR UPDATE OF daily_track ON xenon.uw_flow_events FOR EACH ROW EXECUTE FUNCTION xenon.fanout_uw_flow_event_ticks()
    """,
    """
ALTER TABLE ONLY xenon.uw_analyze_flow_alerts
    ADD CONSTRAINT uw_analyze_flow_alerts_snapshot_id_fkey FOREIGN KEY (snapshot_id) REFERENCES xenon.uw_analyze_snapshots(id) ON DELETE CASCADE
    """,
    """
ALTER TABLE ONLY xenon.uw_analyze_gex_strikes
    ADD CONSTRAINT uw_analyze_gex_strikes_snapshot_id_fkey FOREIGN KEY (snapshot_id) REFERENCES xenon.uw_analyze_snapshots(id) ON DELETE CASCADE
    """,
    """
ALTER TABLE ONLY xenon.uw_analyze_short_volume_trend
    ADD CONSTRAINT uw_analyze_short_volume_trend_snapshot_id_fkey FOREIGN KEY (snapshot_id) REFERENCES xenon.uw_analyze_snapshots(id) ON DELETE CASCADE
    """,
    """
ALTER TABLE ONLY xenon.uw_flow_event_ticks
    ADD CONSTRAINT uw_flow_event_ticks_event_id_fkey FOREIGN KEY (event_id) REFERENCES xenon.uw_flow_events(id) ON DELETE CASCADE
    """,
)


def upgrade() -> None:
    for view in DROPPED_VIEWS:
        op.execute(f"DROP VIEW IF EXISTS xenon.{view}")
    # Dropping a table also drops its triggers, indexes, FKs and owned sequences.
    for table in DROPPED_TABLES:
        op.execute(f"DROP TABLE IF EXISTS xenon.{table}")
    # Triggers are gone with the tables; the functions can now go.
    for fn in DROPPED_FUNCTIONS:
        op.execute(f"DROP FUNCTION IF EXISTS xenon.{fn}()")


def downgrade() -> None:
    # Structure only, no data. Functions first: the triggers in _TABLE_DDL need them.
    for ddl in _FANOUT_FUNCTIONS:
        op.execute(sa.DDL(ddl))
    for ddl in _TABLE_DDL:
        op.execute(sa.DDL(ddl))
    op.execute(sa.DDL(_REGIME_STATE_VIEW))
