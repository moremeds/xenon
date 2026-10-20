"""Test-only adapters with the pre-PR-10 call shapes, over the cores.

PR-10 removed the ``xenon-ib-order-manage`` CLI. The core suites below keep
their bodies: these adapters run ``cancel_order_on_client`` /
``modify_order_on_client`` / ``_locate_trade`` and emit the reply the old
way (JSON on stdout + ``SystemExit`` 0/1), so the identity, classification
and confirmation assertions still read the same reply. ``host`` / ``port``
are accepted and ignored: a connected client never reconnects; an order
owned by another clientId is refused (``classification: ownership``).
"""

from __future__ import annotations

import json
from typing import NoReturn

from xenon.execution import ib_order_manage as iom


def _emit(result: dict) -> NoReturn:
    print(json.dumps(result))
    raise SystemExit(0 if result["status"] == "ok" else 1)


def find_trade(client, order_id, perm_id, *, account=None, client_id=None):
    try:
        return iom._locate_trade(client, order_id, perm_id, account=account, client_id=client_id)
    except iom._Reply as r:
        _emit(iom._result(r.status, r.message, r.extra))


def cancel_order(client, order_id, perm_id, host=None, port=None, account=None, client_id=None) -> NoReturn:
    _emit(iom.cancel_order_on_client(client, order_id, perm_id, account=account, client_id=client_id))


def modify_order(
    client,
    order_id,
    perm_id,
    new_price,
    host=None,
    port=None,
    outside_rth=None,
    new_quantity=None,
    account=None,
    client_id=None,
    non_guaranteed=None,
    send_by=None,
) -> NoReturn:
    _emit(
        iom.modify_order_on_client(
            client,
            order_id,
            perm_id,
            new_price,
            outside_rth=outside_rth,
            new_quantity=new_quantity,
            account=account,
            client_id=client_id,
            non_guaranteed=non_guaranteed,
            send_by=send_by,
        )
    )
