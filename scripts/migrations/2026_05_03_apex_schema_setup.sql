-- 2026_05_03_apex_schema_setup.sql
--
-- Phase 2.5 of the multi-service Postgres migration.
-- Run on the remote (192.168.50.47) AFTER xenon data has been restored.
--
-- Creates the apex_app role and apex schema, plus the cross-service grants
-- that let apex read selected xenon tables (whitelist, not blanket SELECT)
-- and write to the shared events.outbox.
--
-- Idempotent: safe to re-run; uses IF NOT EXISTS / DO blocks where required.
--
-- Design: docs/plans/2026-05-03-multi-service-postgres-design.md
--
-- Run as superuser (postgres):
--   psql -h 192.168.50.47 -U postgres core -v apex_password='<set>' \
--        -f scripts/migrations/2026_05_03_apex_schema_setup.sql

\set ON_ERROR_STOP on

BEGIN;

-- 1. apex_app role (idempotent via DO block)
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'apex_app') THEN
        EXECUTE format('CREATE ROLE apex_app LOGIN PASSWORD %L', :'apex_password');
        RAISE NOTICE 'Created role apex_app';
    ELSE
        RAISE NOTICE 'Role apex_app already exists; password unchanged';
    END IF;
END$$;

-- 2. apex schema, owned by apex_app
CREATE SCHEMA IF NOT EXISTS apex AUTHORIZATION apex_app;

-- 3. Default privileges so future apex-created tables are accessible to apex_app
ALTER DEFAULT PRIVILEGES FOR ROLE apex_app IN SCHEMA apex
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO apex_app;

ALTER DEFAULT PRIVILEGES FOR ROLE apex_app IN SCHEMA apex
    GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO apex_app;

-- 4. Whitelist read-only access to specific xenon tables
--    (NOT GRANT ON ALL TABLES — every addition must be reviewed)
GRANT USAGE ON SCHEMA xenon TO apex_app;
GRANT SELECT ON xenon.account_snapshots TO apex_app;
GRANT SELECT ON xenon.order_submissions TO apex_app;
GRANT SELECT ON xenon.order_fills      TO apex_app;

-- regime_state may not exist yet in older schemas — skip silently if missing
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'xenon' AND table_name = 'regime_state'
    ) THEN
        EXECUTE 'GRANT SELECT ON xenon.regime_state TO apex_app';
    END IF;
END$$;

-- 5. Shared event channel — both apex and xenon emit + consume
GRANT USAGE ON SCHEMA events TO apex_app, xenon_app;
GRANT SELECT, INSERT ON events.outbox TO apex_app, xenon_app;

-- Sequence for events.outbox.id (BIGSERIAL)
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_class c
        JOIN pg_namespace n ON c.relnamespace = n.oid
        WHERE c.relname = 'outbox_id_seq' AND n.nspname = 'events'
    ) THEN
        EXECUTE 'GRANT USAGE ON SEQUENCE events.outbox_id_seq TO apex_app, xenon_app';
    END IF;
END$$;

COMMIT;

-- 6. Audit verification — print final state
\echo
\echo '=== apex_app role ==='
\du+ apex_app

\echo
\echo '=== apex schema ==='
\dn+ apex

\echo
\echo '=== Grants apex_app has on xenon schema ==='
SELECT grantee, table_name, string_agg(privilege_type, ', ' ORDER BY privilege_type) AS privileges
FROM information_schema.role_table_grants
WHERE grantee = 'apex_app' AND table_schema = 'xenon'
GROUP BY grantee, table_name
ORDER BY table_name;

\echo
\echo '=== Grants apex_app has on events schema ==='
SELECT grantee, table_name, string_agg(privilege_type, ', ' ORDER BY privilege_type) AS privileges
FROM information_schema.role_table_grants
WHERE grantee = 'apex_app' AND table_schema = 'events'
GROUP BY grantee, table_name
ORDER BY table_name;
