# Position-Rules 14-Day Clean-Operation Acceptance Gate

Every box must be ticked before flipping `XENON_POSITION_RULES_ENABLED=1` on the live account.

## Daily Checklist

Run for 14 consecutive paper trading days:

```bash
DAY=$(date -u +%F)
uv run python scripts/checks/no_duplicate_close_audit.py --since 1d > "logs/no-dup-close-${DAY}.json"
xenon-position-rules events --since 24h > "logs/transitions-${DAY}.json"
xenon-position-rules health --json > "logs/health-${DAY}.json"
```

Or run the equivalent helper:

```bash
uv run python scripts/checks/position_rules_acceptance_snapshot.py
```

Record the daily result:

| Day | Date | DLQ count | FAILED rules | Triggers auto/alert | Reviewer | Verdict |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | | | | | | |
| 2 | | | | | | |
| 3 | | | | | | |
| 4 | | | | | | |
| 5 | | | | | | |
| 6 | | | | | | |
| 7 | | | | | | |
| 8 | | | | | | |
| 9 | | | | | | |
| 10 | | | | | | |
| 11 | | | | | | |
| 12 | | | | | | |
| 13 | | | | | | |
| 14 | | | | | | |

For every trigger, annotate review state:

```bash
xenon-position-rules review \
  --event-id <id> \
  --protection-id <pid> \
  --reviewed-by <operator> \
  --verdict expected|unexpected|structural \
  --note "..."
```

## Final Gate Criteria

- [ ] Zero rows reached `FAILED` for non-structural reasons. `naked_short_blocked` and `corporate_action_suspected` count as structural.
- [ ] Zero unexpected triggers. Every `verdict='unexpected'` annotation is investigated and resolved.
- [ ] Zero duplicate MKT closes: `uv run python scripts/checks/no_duplicate_close_audit.py --since 14d` returns `violations: []`.
- [ ] Zero arm-consumer DLQ events: `health.outbox_dlq_count == 0` every day.
- [ ] At least one successful trigger -> MKT flatten -> `CLOSED` cycle observed.
- [ ] At least one successful boot reconcile observed after daemon restart with ARMED rows and an in-flight claim.
- [ ] At least one successful native-bracket attach plus per-tick liveness check observed.
- [ ] At least one subprocess-timeout-after-broker-accept case attaches the existing `perm_id` instead of resubmitting.
- [ ] `unprotected_position_count` returns to zero within one daily sweep cycle of every out-of-band fill.
- [ ] Aggregate RTH quote staleness skip rate is below 5%: `stale_quote_skips_last_hour / rule_counts_by_state.ARMED`.
- [ ] `docs/reference/order-path-incident-history.md` has a row for this design.

## Live Promotion

Only after every final gate is green:

```bash
export XENON_POSITION_RULES_ENABLED=1
# restart monitor daemon on the live host
```

Run S1-S11 from `docs/runbooks/position-rules-paper-smoke.md` against the live account with the smallest possible position size for the first three trading days. Continue daily review for at least one additional week before declaring v1 live-stable.

## Sign-Off

- Operator:
- Paper window:
- Successful trigger protection_id:
- Duplicate-close audit artifact:
- Decision: proceed to live / block on follow-up
