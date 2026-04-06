"""Tests for trade_blotter formatting — verify single source of truth."""
import pytest
from decimal import Decimal


class TestFormattingModule:
    """Verify formatting functions come from a single module."""

    def test_format_currency_positive(self):
        from trade_blotter.formatting import format_currency
        assert format_currency(Decimal("1234.56")) == "$1,234.56"

    def test_format_currency_negative(self):
        from trade_blotter.formatting import format_currency
        assert format_currency(Decimal("-500.00")) == "-$500.00"

    def test_format_currency_zero(self):
        from trade_blotter.formatting import format_currency
        assert format_currency(Decimal("0")) == "$0.00"

    def test_format_pnl_positive(self):
        from trade_blotter.formatting import format_pnl
        result = format_pnl(Decimal("100.00"))
        assert "$100.00" in result

    def test_format_pnl_negative(self):
        from trade_blotter.formatting import format_pnl
        result = format_pnl(Decimal("-50.00"))
        assert "$50.00" in result

    def test_format_pnl_zero(self):
        from trade_blotter.formatting import format_pnl
        result = format_pnl(Decimal("0"))
        assert "$0.00" in result


class TestCliImportsFromFormatting:
    """Verify cli.py uses formatting module (same behavior, no local defs)."""

    def test_cli_format_currency_matches(self):
        from trade_blotter.formatting import format_currency as fmt_orig
        from trade_blotter.cli import format_currency as fmt_cli
        # Verify same behavior on a range of values
        for val in [Decimal("0"), Decimal("1234.56"), Decimal("-99.99")]:
            assert fmt_orig(val) == fmt_cli(val)

    def test_cli_format_pnl_matches(self):
        from trade_blotter.formatting import format_pnl as fmt_orig
        from trade_blotter.cli import format_pnl as fmt_cli
        for val in [Decimal("0"), Decimal("100"), Decimal("-50")]:
            assert fmt_orig(val) == fmt_cli(val)

    def test_cli_has_no_local_format_currency_def(self):
        """cli.py should not define its own format_currency."""
        import inspect
        from trade_blotter import cli as cli_mod
        source = inspect.getsource(cli_mod)
        # The only occurrence should be in the import line, not a def
        assert "def format_currency" not in source


class TestFlexQueryImportsFromFormatting:
    """Verify flex_query.py uses formatting module."""

    def test_flex_format_currency_matches(self):
        from trade_blotter.formatting import format_currency as fmt_orig
        from trade_blotter.flex_query import format_currency as fmt_flex
        for val in [Decimal("0"), Decimal("1234.56"), Decimal("-99.99")]:
            assert fmt_orig(val) == fmt_flex(val)

    def test_flex_format_pnl_matches(self):
        from trade_blotter.formatting import format_pnl as fmt_orig
        from trade_blotter.flex_query import format_pnl as fmt_flex
        for val in [Decimal("0"), Decimal("100"), Decimal("-50")]:
            assert fmt_orig(val) == fmt_flex(val)

    def test_flex_has_no_local_format_currency_def(self):
        """flex_query.py should not define its own format_currency."""
        import inspect
        from trade_blotter import flex_query as fq_mod
        source = inspect.getsource(fq_mod)
        assert "def format_currency" not in source
