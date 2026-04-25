from __future__ import annotations

from pathlib import Path

from xenon.execution.orders_store import _connect_utc, _resolve_path

_CREATE_SESSIONS = """
CREATE TABLE IF NOT EXISTS wizard_sessions (
    session_id          TEXT PRIMARY KEY,
    ticker              TEXT NOT NULL,
    state               TEXT NOT NULL,
    structure_name      TEXT NOT NULL,
    intent              TEXT NOT NULL,
    payload_json        JSON,
    current_attempt_id  TEXT,
    created_at          TIMESTAMP NOT NULL,
    updated_at          TIMESTAMP NOT NULL
);
"""

_CREATE_ATTEMPTS = """
CREATE TABLE IF NOT EXISTS wizard_combo_attempts (
    attempt_id          TEXT PRIMARY KEY,
    session_id          TEXT NOT NULL,
    client_attempt_id   TEXT NOT NULL,
    ib_order_id         TEXT,
    perm_id             TEXT,
    intent              TEXT NOT NULL,
    target_price        DECIMAL(18,4) NOT NULL,
    price_basis         TEXT NOT NULL,
    ladder_step         DECIMAL(18,4),
    submitted_at        TIMESTAMP,
    terminal_state      TEXT NOT NULL,
    filled_qty          INTEGER NOT NULL DEFAULT 0,
    avg_fill_price      DECIMAL(18,4),
    ib_reject_code      INTEGER,
    ib_reject_text      TEXT,
    modify_sequence     INTEGER NOT NULL DEFAULT 0,
    created_at          TIMESTAMP NOT NULL,
    updated_at          TIMESTAMP NOT NULL,
    UNIQUE (session_id, client_attempt_id)
);
"""

_CREATE_EVENTS = """
CREATE TABLE IF NOT EXISTS wizard_session_events (
    event_id            TEXT PRIMARY KEY,
    session_id          TEXT NOT NULL,
    kind                TEXT NOT NULL,
    detail              JSON,
    "at"                TIMESTAMP NOT NULL
);
"""

_CREATE_PROTECTION = """
CREATE TABLE IF NOT EXISTS wizard_protection (
    session_id                  TEXT PRIMARY KEY,
    tp_enabled                  BOOLEAN NOT NULL DEFAULT FALSE,
    tp_target_price             DECIMAL(18,4),
    tp_ib_order_id              TEXT,
    alert_enabled               BOOLEAN NOT NULL DEFAULT FALSE,
    alert_net_mid_threshold     DECIMAL(18,4),
    alert_virtual_id            TEXT,
    time_stop_dte               INTEGER,
    created_at                  TIMESTAMP NOT NULL,
    updated_at                  TIMESTAMP NOT NULL
);
"""

_CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_wizard_sessions_state ON wizard_sessions(state, ticker);",
    "CREATE INDEX IF NOT EXISTS ix_wizard_attempts_session ON wizard_combo_attempts(session_id, created_at);",
    'CREATE INDEX IF NOT EXISTS ix_wizard_events_session ON wizard_session_events(session_id, "at");',
]

_MIGRATIONS = [
    "ALTER TABLE wizard_combo_attempts ADD COLUMN IF NOT EXISTS modify_sequence INTEGER DEFAULT 0;",
]


def init_store(db_path: Path | str | None = None) -> Path:
    path = _resolve_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = _connect_utc(path)
    try:
        con.execute(_CREATE_SESSIONS)
        con.execute(_CREATE_ATTEMPTS)
        con.execute(_CREATE_EVENTS)
        con.execute(_CREATE_PROTECTION)
        for stmt in _CREATE_INDEXES:
            con.execute(stmt)
        for stmt in _MIGRATIONS:
            con.execute(stmt)
    finally:
        con.close()
    return path


def list_tables(db_path: Path | str | None = None) -> set[str]:
    con = _connect_utc(_resolve_path(db_path))
    try:
        return {row[0] for row in con.execute("SHOW TABLES").fetchall()}
    finally:
        con.close()
