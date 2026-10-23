"""scripts/checks/order_path_caller_allowlist.py must see multiline imports.

The guard used to match imports line by line, so
``from xenon.execution import (\\n    ib_place_order,\\n)`` slipped through.
These tests run the guard CLI against a throwaway repo root.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

GUARD = Path(__file__).resolve().parents[1] / "checks" / "order_path_caller_allowlist.py"

MULTILINE_PACKAGE_IMPORT = "from xenon.execution import (\n    ib_place_order,\n)\n"
MULTILINE_MODULE_IMPORT = (
    "from xenon.execution.ib_order_manage import (\n    cancel_order_on_client,\n    modify_order_on_client,\n)\n"
)


def _run(root: Path, files: dict[str, str]) -> subprocess.CompletedProcess[str]:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(GUARD), "--repo-root", str(root)],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    "source",
    [
        MULTILINE_PACKAGE_IMPORT,
        MULTILINE_MODULE_IMPORT,
        "import xenon.execution.ib_place_order as placer\n",
        "from xenon.execution.ib_place_order import place_order_on_client\n",
        "from xenon.execution import orders_store, ib_order_manage\n",
        "def late():\n    from xenon.execution import (\n        ib_place_order,\n    )\n",
    ],
)
def test_unauthorized_import_fails(tmp_path: Path, source: str) -> None:
    result = _run(tmp_path, {"src/xenon/api/sneaky.py": source})
    assert result.returncode == 1, result.stdout + result.stderr
    assert "src/xenon/api/sneaky.py" in result.stderr


def test_relative_multiline_import_fails(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        {"src/xenon/execution/other.py": "from . import (\n    ib_place_order,\n)\n"},
    )
    assert result.returncode == 1, result.stdout + result.stderr


def test_allowlisted_caller_may_import_multiline(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        {
            "src/xenon/api/services/order_entry.py": MULTILINE_PACKAGE_IMPORT + MULTILINE_MODULE_IMPORT,
            "src/xenon/execution/ib_place_order.py": "",
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_test_files_are_exempt(tmp_path: Path) -> None:
    result = _run(tmp_path, {"src/xenon/api/tests/test_x.py": MULTILINE_PACKAGE_IMPORT})
    assert result.returncode == 0, result.stdout + result.stderr


def test_unrelated_multiline_import_passes(tmp_path: Path) -> None:
    source = "from xenon.execution import (\n    orders_store,\n    preflight,\n)\n"
    result = _run(tmp_path, {"src/xenon/api/fine.py": source})
    assert result.returncode == 0, result.stdout + result.stderr


def test_import_text_in_docstring_and_comment_passes(tmp_path: Path) -> None:
    source = (
        '"""Only the order owner may do:\n\n'
        "    from xenon.execution import (\n        ib_place_order,\n    )\n"
        '"""\n'
        "# from xenon.execution.ib_place_order import place_order_on_client\n"
    )
    result = _run(tmp_path, {"src/xenon/api/docs_only.py": source})
    assert result.returncode == 0, result.stdout + result.stderr


def test_removed_cli_name_still_fails_everywhere(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        {"pyproject.toml": '[project.scripts]\nxenon-ib-place-order = "x:main"\n'},
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "pyproject.toml" in result.stderr


def test_removed_cli_name_in_python_comment_is_ignored(tmp_path: Path) -> None:
    result = _run(tmp_path, {"src/xenon/api/note.py": "# xenon-ib-place-order was removed\n"})
    assert result.returncode == 0, result.stdout + result.stderr


def test_non_python_file_keeps_text_match(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        {"scripts/run.sh": "python -c 'x'\nfrom xenon.execution.ib_place_order import place_order_on_client\n"},
    )
    assert result.returncode == 1, result.stdout + result.stderr


def test_unparseable_python_falls_back_to_line_match(tmp_path: Path) -> None:
    source = "from xenon.execution.ib_place_order import place_order_on_client\ndef broken(:\n"
    result = _run(tmp_path, {"src/xenon/api/broken.py": source})
    assert result.returncode == 1, result.stdout + result.stderr


@pytest.mark.parametrize("module", ["ib_place_order", "ib_order_manage"])
@pytest.mark.parametrize("prefix", ["..execution", ".execution", ".."])
def test_qualified_relative_module_import_fails(tmp_path: Path, module: str, prefix: str) -> None:
    source = f"from {prefix}.{module} import (\n    place_order_on_client,\n)\n" if prefix != ".." else (
        f"from ..{module} import place_order_on_client\n"
    )
    result = _run(tmp_path, {"src/xenon/api/example.py": source})
    assert result.returncode == 1, result.stdout + result.stderr


def test_real_repo_is_green() -> None:
    result = subprocess.run(
        [sys.executable, str(GUARD), "--repo-root", str(GUARD.parents[2])],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# ── Futu adapter target: xenon.execution.futu_orders ────────────


FUTU_ADAPTER = "src/xenon/execution/futu_orders.py"


@pytest.mark.parametrize(
    "source",
    [
        "from xenon.execution.futu_orders import place_futu_order\n",
        "import xenon.execution.futu_orders\n",
        "import xenon.execution.futu_orders as fo\n",
        "from xenon.execution import futu_orders\n",
        "from xenon.execution import orders_store, futu_orders\n",
        "from xenon.execution import (\n    orders_store,\n    futu_orders,\n)\n",
        "from xenon.execution.futu_orders import (\n    place_futu_order,\n    cancel_futu_order,\n)\n",
        "def late():\n    from xenon.execution import (\n        futu_orders,\n    )\n",
        "from ..execution.futu_orders import place_futu_order\n",
        "from ..execution import (\n    futu_orders,\n)\n",
    ],
)
def test_unauthorized_futu_orders_import_fails(tmp_path: Path, source: str) -> None:
    result = _run(tmp_path, {"src/xenon/api/sneaky.py": source})
    assert result.returncode == 1, result.stdout + result.stderr
    assert "src/xenon/api/sneaky.py" in result.stderr


@pytest.mark.parametrize(
    "source",
    ["from . import futu_orders\n", "from .futu_orders import place_futu_order\n", "from . import (\n    futu_orders,\n)\n"],
)
def test_relative_futu_orders_import_fails(tmp_path: Path, source: str) -> None:
    result = _run(tmp_path, {"src/xenon/execution/other.py": source})
    assert result.returncode == 1, result.stdout + result.stderr


@pytest.mark.parametrize("rel", ["src/xenon/execution/ib_place_order.py", "src/xenon/execution/ib_order_manage.py"])
def test_ib_allowlist_does_not_cover_futu_orders(tmp_path: Path, rel: str) -> None:
    result = _run(tmp_path, {rel: "from xenon.execution import futu_orders\n"})
    assert result.returncode == 1, result.stdout + result.stderr


def test_order_entry_and_adapter_may_import_futu_orders(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        {
            "src/xenon/api/services/order_entry.py": "from xenon.execution import (\n    futu_orders,\n)\n",
            FUTU_ADAPTER: "from xenon.execution.futu_orders import _helper\n",
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_futu_adapter_may_not_import_ib_cores(tmp_path: Path) -> None:
    result = _run(tmp_path, {FUTU_ADAPTER: "from xenon.execution import ib_place_order\n"})
    assert result.returncode == 1, result.stdout + result.stderr


def test_unparseable_python_futu_orders_import_falls_back(tmp_path: Path) -> None:
    source = "from xenon.execution.futu_orders import place_futu_order\ndef broken(:\n"
    result = _run(tmp_path, {"src/xenon/api/broken.py": source})
    assert result.returncode == 1, result.stdout + result.stderr


# ── Raw Futu SDK mutations outside the adapter ──────────────────

_MUTATIONS = ("place_order", "modify_order", "cancel_all_order", "unlock_trade")


@pytest.mark.parametrize("method", _MUTATIONS)
@pytest.mark.parametrize(
    "template",
    [
        "from futu import OpenSecTradeContext\nctx = OpenSecTradeContext(host='h', port=1)\nctx.{m}(1)\n",
        "import futu\nctx = futu.OpenSecTradeContext()\nctx.{m}(1)\n",
        "import futu as ft\nctx = ft.OpenSecTradeContext()\nctx.{m}(1)\n",
        "from futu import OpenSecTradeContext as T\nc = T()\nc.{m}(1)\n",
        "from futu import OpenSecTradeContext\nwith OpenSecTradeContext() as c:\n    c.{m}(1)\n",
        "from futu import OpenSecTradeContext\nOpenSecTradeContext().{m}(1)\n",
        "from futu import OpenFutureTradeContext\nc = OpenFutureTradeContext()\nc.{m}(1)\n",
        (
            "from futu import OpenSecTradeContext\nclass C:\n    def connect(self):\n"
            "        self._ctx = OpenSecTradeContext()\n    def go(self):\n        self._ctx.{m}(1)\n"
        ),
        "from futu import OpenSecTradeContext\nctx = OpenSecTradeContext()\nalias = ctx\nalias.{m}(1)\n",
        "from futu import OpenSecTradeContext\nctx: OpenSecTradeContext = OpenSecTradeContext()\nctx.{m}(\n    1,\n)\n",
        "from futu import OpenSecTradeContext\nclass Mine(OpenSecTradeContext):\n    pass\nm = Mine()\nm.{m}(1)\n",
        "from futu import OpenSecTradeContext\nctx = OpenSecTradeContext()\ngetattr(ctx, '{m}')(1)\n",
    ],
)
def test_raw_futu_mutation_outside_adapter_fails(tmp_path: Path, method: str, template: str) -> None:
    result = _run(tmp_path, {"src/xenon/clients/raw.py": template.format(m=method)})
    assert result.returncode == 1, result.stdout + result.stderr
    assert "src/xenon/clients/raw.py" in result.stderr


@pytest.mark.parametrize("method", _MUTATIONS)
def test_raw_mutation_on_another_files_context_attr_fails(tmp_path: Path, method: str) -> None:
    """A context created in one file (FutuClient._trd_ctx) and used in another."""
    result = _run(
        tmp_path,
        {
            "src/xenon/clients/futu_client.py": (
                "from futu import OpenSecTradeContext\nclass FutuClient:\n    def connect(self):\n"
                "        self._trd_ctx = OpenSecTradeContext()\n"
            ),
            "src/xenon/api/routes/sneaky.py": f"def go(client):\n    client._trd_ctx.{method}(1)\n",
        },
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "src/xenon/api/routes/sneaky.py" in result.stderr
    assert "futu_client.py" not in result.stderr


def test_mutation_added_to_real_futu_client_fails(tmp_path: Path) -> None:
    """The repo's own FutuClient (read-only today) is caught if it gains a send."""
    real = (GUARD.parents[2] / "src/xenon/clients/futu_client.py").read_text(encoding="utf-8")
    assert real.count("self._trd_ctx = OpenSecTradeContext(") == 1
    clean = _run(tmp_path, {"src/xenon/clients/futu_client.py": real})
    assert clean.returncode == 0, clean.stdout + clean.stderr
    sneaky = real + "\n\ndef _sneaky(client):\n    return client._trd_ctx.place_order(1, 1, 'US.AAPL', 'BUY')\n"
    result = _run(tmp_path, {"src/xenon/clients/futu_client.py": sneaky})
    assert result.returncode == 1, result.stdout + result.stderr
    assert "client._trd_ctx.place_order" in result.stderr


@pytest.mark.parametrize("method", _MUTATIONS)
def test_adapter_may_call_futu_mutations(tmp_path: Path, method: str) -> None:
    source = f"from futu import OpenSecTradeContext\nctx = OpenSecTradeContext()\nctx.{method}(1)\n"
    result = _run(tmp_path, {FUTU_ADAPTER: source})
    assert result.returncode == 0, result.stdout + result.stderr


def test_read_only_futu_calls_pass(tmp_path: Path) -> None:
    source = (
        "from futu import OpenSecTradeContext\nctx = OpenSecTradeContext()\n"
        "ctx.get_acc_list()\nctx.position_list_query()\nctx.order_list_query()\n"
    )
    result = _run(tmp_path, {"src/xenon/clients/futu_client.py": source})
    assert result.returncode == 0, result.stdout + result.stderr


def test_ib_place_order_path_is_allowed(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        {
            "src/xenon/execution/ib_place_order.py": "def core(client, contract, order):\n    return client.place_order(contract, order)\n",
            "src/xenon/api/order_owner.py": (
                "class Guarded:\n    def place_order(self, *a, **k):\n        return self._client.place_order(*a, **k)\n"
            ),
            "src/xenon/execution/ib_order_manage.py": "def m(client, trade):\n    client.place_order(trade.contract, trade.order)\n",
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_ib_client_next_to_futu_context_is_allowed(tmp_path: Path) -> None:
    source = (
        "from futu import OpenSecTradeContext\nctx = OpenSecTradeContext()\nctx.get_acc_list()\n"
        "def place(ib_client, c, o):\n    return ib_client.place_order(c, o)\n"
    )
    result = _run(tmp_path, {"src/xenon/api/mixed.py": source})
    assert result.returncode == 0, result.stdout + result.stderr


def test_futu_mutation_in_test_file_is_exempt(tmp_path: Path) -> None:
    source = "from futu import OpenSecTradeContext\nctx = OpenSecTradeContext()\nctx.place_order(1)\n"
    result = _run(tmp_path, {"scripts/tests/test_fake.py": source})
    assert result.returncode == 0, result.stdout + result.stderr


def test_unparseable_file_mutation_is_documented_gap(tmp_path: Path) -> None:
    """The invalid-Python mutation fallback was removed: a Futu mutation in a
    file that does not parse is a documented blind spot. The separate IMPORT
    fallback for unparseable files still stands (see test above)."""
    source = "from futu import OpenSecTradeContext\nctx = OpenSecTradeContext()\nctx.place_order(1)\ndef broken(:\n"
    result = _run(tmp_path, {"src/xenon/clients/broken.py": source})
    assert result.returncode == 0, result.stdout + result.stderr


# ── F1 regressions: alias of a cross-file context attr; bound-method capture ──


def test_ctx_alias_of_cross_file_attr_fails(tmp_path: Path) -> None:
    """`ctx = client._trd_ctx` then `ctx.place_order(1)` — an ordinary alias of
    the real FutuClient's context attribute keeps its Futu identity."""
    real = (GUARD.parents[2] / "src/xenon/clients/futu_client.py").read_text(encoding="utf-8")
    result = _run(
        tmp_path,
        {
            "src/xenon/clients/futu_client.py": real,
            "src/xenon/api/routes/sneaky.py": (
                "def go(client):\n    ctx = client._trd_ctx\n    return ctx.place_order(1)\n"
            ),
        },
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "src/xenon/api/routes/sneaky.py" in result.stderr


def test_bound_method_capture_of_futu_mutation_fails(tmp_path: Path) -> None:
    """`send = ctx.place_order` — referencing a mutation attribute on a known
    Futu context is flagged even without a call."""
    source = (
        "from futu import OpenSecTradeContext\n"
        "ctx = OpenSecTradeContext()\n"
        "send = ctx.place_order\n"
        "send(1)\n"
    )
    result = _run(tmp_path, {"src/xenon/clients/raw.py": source})
    assert result.returncode == 1, result.stdout + result.stderr
    assert "ctx.place_order" in result.stderr


@pytest.mark.parametrize("method", _MUTATIONS)
def test_bound_method_capture_each_mutation_fails(tmp_path: Path, method: str) -> None:
    source = f"from futu import OpenSecTradeContext\nctx = OpenSecTradeContext()\nsend = ctx.{method}\nsend(1)\n"
    result = _run(tmp_path, {"src/xenon/clients/raw.py": source})
    assert result.returncode == 1, result.stdout + result.stderr


def test_ib_client_alias_mutation_passes(tmp_path: Path) -> None:
    """Negative control: an ordinary alias of a non-Futu client stays allowed —
    no declared Futu context attribute anywhere in the scan."""
    source = "def go(client):\n    ctx = client\n    return ctx.place_order(1)\n"
    result = _run(tmp_path, {"src/xenon/execution/helper.py": source})
    assert result.returncode == 0, result.stdout + result.stderr


def test_show_allowlist_lists_both_targets(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(GUARD), "--show-allowlist"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0
    assert "futu_orders" in result.stdout and FUTU_ADAPTER in result.stdout
    assert "src/xenon/api/services/order_entry.py" in result.stdout
