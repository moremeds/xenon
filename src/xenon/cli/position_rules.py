"""Operator CLI for position rules."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from xenon.api.services.position_rules_health import compute_health
from xenon.db.engine import get_sync_engine
from xenon.db.queries.position_protection import cas_transition, get_by_id, list_active_rows
from xenon.db.queries.position_rules_review import add_annotation
from xenon.execution.account_scope import resolve_from_env

_ACTIVE_STATES = ("PENDING_ARM", "ARMED", "TRIGGERED")


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json_safe(inner) for key, inner in value.items()}
    if isinstance(value, list):
        return [_json_safe(inner) for inner in value]
    return value


def _print_json(payload: Any) -> None:
    print(json.dumps(_json_safe(payload), default=str))


def _cmd_list(args: argparse.Namespace) -> int:
    scope = resolve_from_env()
    engine = get_sync_engine()
    rows = list_active_rows(
        engine,
        broker=scope.broker,
        account_env=scope.account_env,
        broker_account=scope.broker_account,
        states=tuple(args.state) if args.state else _ACTIVE_STATES,
    )
    if args.rule_kind:
        rows = [row for row in rows if row["rule_kind"] in args.rule_kind]
    if args.json:
        _print_json(rows)
    else:
        for row in rows:
            print(f"{row['protection_id']:>6} {row['rule_kind']:<18} {row['state']:<12} {row['position_key']}")
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    row = get_by_id(get_sync_engine(), protection_id=args.protection_id)
    if row is None:
        print(f"protection_id={args.protection_id} not found", file=sys.stderr)
        return 1
    if args.json:
        _print_json(row)
    else:
        for key, value in row.items():
            print(f"{key:<25} {value}")
    return 0


def _cmd_cancel(args: argparse.Namespace) -> int:
    engine = get_sync_engine()
    row = get_by_id(engine, protection_id=args.protection_id)
    if row is None:
        print(f"protection_id={args.protection_id} not found", file=sys.stderr)
        return 1
    if row["state"] not in _ACTIVE_STATES and not args.force:
        print(f"row is already terminal ({row['state']}); use --force to override", file=sys.stderr)
        return 1
    if not cas_transition(
        engine,
        protection_id=args.protection_id,
        expected_state=row["state"],
        new_state="CANCELED",
        reason="operator_cancel_cli",
    ):
        print("CAS transition failed", file=sys.stderr)
        return 1
    print(f"canceled protection_id={args.protection_id}")
    return 0


def _positions_from_ib() -> list[dict[str, Any]]:
    from xenon.clients.ib_client import IBClient

    singleton = getattr(IBClient, "singleton", None)
    client = singleton() if callable(singleton) else IBClient()
    if hasattr(client, "positions"):
        return list(client.positions() or [])
    if hasattr(client, "get_positions"):
        out: list[dict[str, Any]] = []
        for pos in client.get_positions() or []:
            contract = getattr(pos, "contract", None)
            out.append(
                {
                    "symbol": getattr(contract, "symbol", None),
                    "qty": getattr(pos, "position", 0),
                    "con_id": getattr(contract, "conId", None),
                }
            )
        return out
    return []


def _cmd_sweep(args: argparse.Namespace) -> int:
    scope = resolve_from_env()
    if args.apply and scope.account_env == "live" and not os.environ.get("XENON_OPERATOR_USER_ID"):
        print(
            json.dumps(
                {
                    "reason_code": "live_trading_auth_unconfigured",
                    "message": "XENON_OPERATOR_USER_ID must be set for live --apply",
                }
            ),
            file=sys.stderr,
        )
        return 1

    engine = get_sync_engine()
    candidates: list[dict[str, Any]] = []
    for position in _positions_from_ib():
        symbol = position.get("symbol")
        if not symbol:
            continue
        with engine.connect() as conn:
            existing = conn.execute(
                text(
                    """
                    SELECT 1 FROM xenon.position_protection
                    WHERE broker = :broker
                      AND account_env = :account_env
                      AND broker_account = :broker_account
                      AND position_key LIKE :position_key
                      AND state IN ('PENDING_ARM','ARMED','TRIGGERED')
                    LIMIT 1
                    """
                ),
                {
                    "broker": scope.broker,
                    "account_env": scope.account_env,
                    "broker_account": scope.broker_account,
                    "position_key": f"%::{symbol}%",
                },
            ).first()
        if existing is None:
            candidates.append(position)

    if not args.apply:
        _print_json({"would_insert": candidates, "count": len(candidates)})
        return 0

    from xenon.execution.brackets.arm_hook import sweep_insert

    inserted = 0
    for candidate in candidates:
        before = len(
            list_active_rows(
                engine,
                broker=scope.broker,
                account_env=scope.account_env,
                broker_account=scope.broker_account,
            )
        )
        sweep_insert(engine, scope=scope, candidate=candidate)
        after = len(
            list_active_rows(
                engine,
                broker=scope.broker,
                account_env=scope.account_env,
                broker_account=scope.broker_account,
            )
        )
        inserted += max(0, after - before)
    _print_json({"applied": inserted, "skipped": len(candidates) - inserted})
    return 0


def _cmd_health(args: argparse.Namespace) -> int:
    body = compute_health(engine=get_sync_engine(), scope=resolve_from_env())
    _print_json(body)
    return 0


def _parse_since(value: str) -> timedelta:
    if value.endswith("m"):
        return timedelta(minutes=int(value[:-1]))
    if value.endswith("h"):
        return timedelta(hours=int(value[:-1]))
    if value.endswith("d"):
        return timedelta(days=int(value[:-1]))
    raise ValueError(f"unrecognized --since: {value!r}")


def _cmd_events(args: argparse.Namespace) -> int:
    cutoff = datetime.now(timezone.utc) - _parse_since(args.since)
    with get_sync_engine().connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT id, payload, emitted_at
                FROM events.outbox
                WHERE channel = 'position_rule.transition'
                  AND emitted_at >= :cutoff
                ORDER BY id DESC
                LIMIT 1000
                """
            ),
            {"cutoff": cutoff},
        ).all()
    _print_json(
        [
            {
                "event_id": row.id,
                "emitted_at": row.emitted_at.isoformat(),
                **(row.payload or {}),
            }
            for row in rows
        ]
    )
    return 0


def _cmd_review(args: argparse.Namespace) -> int:
    review_id = add_annotation(
        get_sync_engine(),
        protection_id=args.protection_id,
        event_id=args.event_id,
        reviewed_by=args.reviewed_by,
        verdict=args.verdict,
        note=args.note,
    )
    _print_json({"review_id": review_id})
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="xenon-position-rules")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list")
    p_list.add_argument("--state", action="append")
    p_list.add_argument("--rule-kind", action="append", dest="rule_kind")
    p_list.add_argument("--json", action="store_true")
    p_list.set_defaults(func=_cmd_list)

    p_show = sub.add_parser("show")
    p_show.add_argument("protection_id", type=int)
    p_show.add_argument("--json", action="store_true")
    p_show.set_defaults(func=_cmd_show)

    p_cancel = sub.add_parser("cancel")
    p_cancel.add_argument("protection_id", type=int)
    p_cancel.add_argument("--force", action="store_true")
    p_cancel.set_defaults(func=_cmd_cancel)

    p_sweep = sub.add_parser("sweep")
    p_sweep.add_argument("--apply", action="store_true")
    p_sweep.add_argument("--dry-run", action="store_true")
    p_sweep.add_argument("--rate-limit-per-min", type=int, default=30)
    p_sweep.set_defaults(func=_cmd_sweep)

    p_health = sub.add_parser("health")
    p_health.add_argument("--json", action="store_true")
    p_health.set_defaults(func=_cmd_health)

    p_events = sub.add_parser("events")
    p_events.add_argument("--since", default="24h")
    p_events.set_defaults(func=_cmd_events)

    p_review = sub.add_parser("review")
    p_review.add_argument("--event-id", type=int, required=True)
    p_review.add_argument("--protection-id", type=int, required=True)
    p_review.add_argument("--reviewed-by", required=True)
    p_review.add_argument("--verdict", choices=("expected", "unexpected", "structural"), required=True)
    p_review.add_argument("--note")
    p_review.set_defaults(func=_cmd_review)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
