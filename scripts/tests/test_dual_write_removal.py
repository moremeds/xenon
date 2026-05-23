"""W5 guardrails: migrated runtime routes must not dual-write JSON caches."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_fastapi_no_longer_dual_writes_migrated_json_caches():
    server_py = (REPO_ROOT / "src/xenon/api/server.py").read_text()
    forbidden = [
        '_write_cache(DATA_DIR / "scanner.json"',
        '_write_cache(DATA_DIR / "discover.json"',
        '_write_cache(DATA_DIR / "cri.json"',
        '_write_cache(DATA_DIR / "vcg.json"',
        '_write_cache(DATA_DIR / "gex.json"',
        '_write_cache(DATA_DIR / "blotter.json"',
        '_write_cache(DATA_DIR / "performance.json"',
    ]
    offenders = [pattern for pattern in forbidden if pattern in server_py]
    assert not offenders, "FastAPI still dual-writes migrated JSON caches: " + ", ".join(offenders)


def test_fastapi_no_longer_reads_orders_json_for_modify_fallback():
    server_py = (REPO_ROOT / "src/xenon/api/server.py").read_text()
    assert '_read_cache(DATA_DIR / "orders.json")' not in server_py


def test_ib_orders_sync_no_longer_writes_orders_json():
    ib_orders_py = (REPO_ROOT / "src/xenon/execution/ib_orders.py").read_text()
    assert "ORDERS_PATH" not in ib_orders_py
    assert "orders.json" not in ib_orders_py


def test_next_migrated_routes_do_not_read_runtime_json_caches():
    route_paths = [
        "web/app/api/blotter/route.ts",
        "web/app/api/performance/route.ts",
        "web/app/api/portfolio/route.ts",
        "web/app/api/journal/route.ts",
        "web/app/api/orders/route.ts",
    ]
    offenders: list[str] = []
    for rel in route_paths:
        text = (REPO_ROOT / rel).read_text()
        if "fs/promises" in text or "readFile(" in text or "readDataFile(" in text:
            offenders.append(rel)
    assert not offenders, "Migrated Next routes still read runtime JSON caches: " + ", ".join(offenders)
