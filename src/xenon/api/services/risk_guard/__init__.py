"""Tail-risk guard (spec docs/superpowers/specs/2026-11-08-tail-risk-guard-design.md).

P1 detects and notifies only. Nothing in this package places, cancels or
modifies an order, and nothing imports the order path.
"""
