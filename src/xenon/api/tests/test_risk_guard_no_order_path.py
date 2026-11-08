"""P1 acceptance: the risk guard reads and notifies only; it never imports the order path."""

import ast
from pathlib import Path

import xenon.api.services.risk_guard as pkg

FORBIDDEN = (
    "xenon.execution.ib_place_order",
    "xenon.execution.ib_order_manage",
    "xenon.execution.futu_orders",
    "xenon.api.order_owner",
    "xenon.api.services.order_entry",
    "xenon.api.futu_order_worker",
)


def test_no_order_path_imports():
    root = Path(pkg.__file__).parent
    hits = []
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
            hits += [f"{path.name}: {n}" for n in names if n.startswith(FORBIDDEN)]
    assert hits == []
