"""Offline tests for the X7-0 calibration script (pure parts only).

Every number below is a SYNTHETIC fixture value chosen to exercise arithmetic.
None of them is a market observation. No network, no OpenD, no IB, no database.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from scripts.research import futu_vs_ib_quotes as m
from xenon.utils.symbol_norm import futu_to_ib

# --- code mapping ---------------------------------------------------------


def test_option_code_matches_futu_code_for_rule_and_round_trips():
    opt = m.parse_option_key("AAPL_20261120_200_C")
    assert m.futu_option_code(opt) == "US.AAPL261120C200000"  # no zero padding, strike x 1000
    back = futu_to_ib(m.futu_option_code(opt))
    assert (back["symbol"], back["expiry"], back["right"], Decimal(str(back["strike"]))) == (
        "AAPL",
        "20261120",
        "C",
        Decimal("200"),
    )


def test_fractional_strike_and_put():
    opt = m.parse_option_key("spy_20261218_512.5_p")
    assert opt.key == "SPY_20261218_512.5_P"
    assert m.futu_option_code(opt) == "US.SPY261218P512500"


@pytest.mark.parametrize(
    "bad", ["AAPL", "AAPL_20261120_200", "AAPL_20261340_200_C", "AAPL_20261120_200.1234_C", "AAPL_20261120_0_C"]
)
def test_bad_option_keys_rejected(bad):
    with pytest.raises(ValueError):
        m.parse_option_key(bad)


def test_stock_code_and_class_share():
    assert m.futu_stock_code("aapl") == "US.AAPL"
    assert m.futu_stock_code("BRK.B") == "US.BRK.B"
    with pytest.raises(ValueError):
        m.futu_stock_code("bad sym")


def test_build_instruments_adds_vertical_legs_once():
    v = m.parse_vertical("AAPL_20261120_200_C:AAPL_20261120_210_C")
    inst = m.build_instruments(["AAPL"], ["AAPL_20261120_200_C"], [v])
    assert [i.key for i in inst] == ["AAPL", "AAPL_20261120_200_C", "AAPL_20261120_210_C"]


def test_plan_limits():
    args = m.build_parser().parse_args(["--stocks", ",".join(f"S{c}" for c in "ABCDEFGHIJKL")])
    with pytest.raises(ValueError, match="IB line budget"):
        m.resolve_plan(args)
    args = m.build_parser().parse_args(
        ["--session", "overnight", "--stocks", "AAPL", "--options", "AAPL_20261120_200_C"]
    )
    with pytest.raises(ValueError, match="stocks only"):
        m.resolve_plan(args)


def test_dry_run_connects_to_nothing(capsys):
    rc = m.main(["--dry-run", "--stocks", "AAPL", "--options", "AAPL_20261120_200_C"])
    out = capsys.readouterr().out
    assert rc == 0 and "US.AAPL261120C200000" in out


# --- cents, mids, combo ---------------------------------------------------


def test_no_tick_rule_left():
    assert not hasattr(m, "tick_size") and not hasattr(m, "diff_ticks")


def test_diff_cents():
    assert m.diff_cents(1.03, 1.00) == pytest.approx(3.0)
    assert m.diff_cents(3.10, 3.00) == pytest.approx(10.0)  # same cents at any price level
    assert m.diff_cents(None, 1.0) is None


def test_combo_mid_sign_long_plus_short_minus():
    # synthetic leg mids: long 5.00, short 2.00 -> debit vertical mid +3.00
    assert m.combo_mid([(+1, 5.0), (-1, 2.0)]) == pytest.approx(3.0)
    assert m.combo_mid([(+1, 5.0), (-1, None)]) is None


def test_to_float_handles_futu_cell_quirks():
    assert m.to_float("N/A") is None
    assert m.to_float(float("nan")) is None
    assert m.to_float([1, 2]) is None
    assert m.to_float("1.5") == 1.5
    assert m.to_float(None) is None


# --- ratios and stats ----------------------------------------------------


def test_greek_ratio_divides_futu_iv_by_100():
    assert m.greek_ratio("iv", 25.0, 0.25) == pytest.approx(1.0)  # synthetic: Futu percent vs IB decimal
    assert m.greek_ratio("delta", 0.5, 0.25) == pytest.approx(2.0)
    assert m.greek_ratio("gamma", 0.1, 0.0) is None
    assert m.greek_ratio("theta", None, 1.0) is None


def test_quantile_and_stat_summary():
    assert m.quantile([1.0, 2.0, 3.0, 4.0, 5.0], 0.5) == 3.0
    assert m.quantile([1.0, 2.0, 3.0, 4.0, 5.0], 0.25) == 2.0
    assert m.quantile([7.0], 0.9) == 7.0
    assert m.quantile([], 0.5) is None
    s = m.stat_summary([1.0, None, 2.0, 3.0, 4.0, 5.0])
    assert (s["n"], s["median"], s["q1"], s["q3"]) == (5, 3.0, 2.0, 4.0)
    a = m.abs_summary([-2.0, 0.5, 1.0, None])
    assert a["n"] == 3 and a["max"] == 2.0
    assert a["within"][1] == pytest.approx(2 / 3) and a["within"][5] == pytest.approx(1.0)
    assert m.abs_summary([5.0, 5.0001])["within"][5] == pytest.approx(0.5)


# --- matched-time join ---------------------------------------------------


def test_series_at_latest_not_after_t():
    s = m.Series()
    s.add(10.0, {"v": 1})
    s.add(20.0, {"v": 2})
    s.add(15.0, {"v": 3})  # out-of-order insert keeps order
    assert s.at(9.9) is None
    assert s.at(10.0) == (10.0, {"v": 1})
    assert s.at(17.0) == (15.0, {"v": 3})
    assert s.at(99.0) == (20.0, {"v": 2})


def _fixture():
    """Synthetic: a stock and a call. Futu lags: its book updates at 100 and 130."""
    stock = m.Instrument("SYN", "STK", "US.SYN")
    opt_o = m.parse_option_key("SYN_20261120_100_C")
    opt = m.Instrument(opt_o.key, "OPT", m.futu_option_code(opt_o), opt_o)
    q, b = {}, {}
    q["US.SYN"], b["US.SYN"] = m.Series(), m.Series()
    q[opt.futu_code], b[opt.futu_code] = m.Series(), m.Series()
    b["US.SYN"].add(100.0, {"bid": 10.00, "ask": 10.02})
    b["US.SYN"].add(130.0, {"bid": 10.05, "ask": 10.07})
    q["US.SYN"].add(99.0, {"last_price": 10.01, "data_date": "2026-10-26", "data_time": "10:00:00"})
    b[opt.futu_code].add(100.0, {"bid": 1.00, "ask": 1.10})
    q[opt.futu_code].add(
        100.0,
        {"last_price": 1.05, "implied_volatility": 25.0, "delta": 0.5, "gamma": 0.04, "vega": 0.1, "theta": -0.02},
    )
    return stock, opt, q, b


def test_compare_sample_uses_latest_push_at_or_before_t():
    stock, _opt, q, b = _fixture()
    sample = {"key": "SYN", "cycle": 0, "t": 110.0, "bid": 10.01, "ask": 10.03}
    row = m.compare_sample(stock, sample, q["US.SYN"], b["US.SYN"])
    assert row["f_bid"] == 10.00 and row["f_ask"] == 10.02  # the 100.0 push, not the 130.0 one
    assert row["d_bid"] == pytest.approx(-1.0) and row["d_ask"] == pytest.approx(-1.0)  # cents
    assert row["d_mid"] == pytest.approx(-1.0)
    assert row["book_age_s"] == pytest.approx(10.0)
    assert row["quote_age_s"] == pytest.approx(11.0)
    assert row["d_last_vs_ib_mid"] == pytest.approx(-1.0)  # last 10.01 vs IB mid 10.02


def test_compare_sample_before_first_push_is_none():
    stock, _opt, q, b = _fixture()
    row = m.compare_sample(
        stock, {"key": "SYN", "cycle": 0, "t": 50.0, "bid": 10.0, "ask": 10.02}, q["US.SYN"], b["US.SYN"]
    )
    assert row["f_mid"] is None and row["d_mid"] is None and row["book_age_s"] is None


def test_option_greek_ratios():
    _stock, opt, q, b = _fixture()
    sample = {
        "key": opt.key,
        "cycle": 0,
        "t": 120.0,
        "bid": 1.00,
        "ask": 1.10,
        "greeks": {"impliedVol": 0.25, "delta": 0.5, "gamma": 0.02, "vega": 0.1, "theta": -0.01},
    }
    row = m.compare_sample(opt, sample, q[opt.futu_code], b[opt.futu_code])
    assert "tick" not in row
    assert row["r_iv"] == pytest.approx(1.0)
    assert row["r_delta"] == pytest.approx(1.0)
    assert row["r_gamma"] == pytest.approx(2.0)
    assert row["r_theta"] == pytest.approx(2.0)


def _leg(key, cycle, t, ib, fu):
    return {"key": key, "cycle": cycle, "t": t, "ib_mid": ib, "f_mid": fu}


def test_combo_gaps_per_cycle_cents():
    rows = [
        _leg("L", 0, 100.0, 5.00, 5.02),
        _leg("S", 0, 102.5, 2.00, 1.99),
        _leg("L", 1, 130.0, 5.00, None),
        _leg("S", 1, 131.0, 2.00, 2.00),
    ]
    out = m.combo_gaps([m.Vertical("L", "S")], rows)["L:S"]
    assert out[0]["ib_spread"] == pytest.approx(3.0) and out[0]["all_futu"] == pytest.approx(3.03)
    assert out[0]["gap_all_futu_c"] == pytest.approx(3.0)  # 3.03 - 3.00 in cents
    assert out[0]["sep_s"] == pytest.approx(2.5)
    assert out[1]["gap_all_futu_c"] is None and out[1]["gap_fl_is_c"] is None and out[1]["gap_il_fs_c"] is None


def test_combo_mixed_orientations_expose_cancelled_leg_errors():
    # synthetic: Futu is 5 cents low on both legs. The errors cancel in the all-Futu spread
    # but not when one leg comes from Futu and the other from IB.
    rows = [_leg("L", 0, 100.0, 5.00, 4.95), _leg("S", 0, 101.0, 2.00, 1.95)]
    c = m.combo_gaps([m.Vertical("L", "S")], rows)["L:S"][0]
    assert c["gap_all_futu_c"] == pytest.approx(0.0)
    assert c["gap_fl_is_c"] == pytest.approx(-5.0)  # Futu long 4.95, IB short 2.00 -> 2.95 vs 3.00
    assert c["gap_il_fs_c"] == pytest.approx(5.0)  # IB long 5.00, Futu short 1.95 -> 3.05 vs 3.00
    summ = m.combo_summary_of({"L:S": [c]})["L:S"]
    assert summ["orient"]["all_futu"]["gap_abs"]["within"][1] == pytest.approx(1.0)
    assert summ["orient"]["fl_is"]["gap_abs"]["within"][1] == pytest.approx(0.0)
    assert summ["orient"]["fl_is"]["gap_abs"]["within"][5] == pytest.approx(1.0)
    assert summ["sep"] == {"n": 1, "min": 1.0, "median": 1.0, "max": 1.0}


def test_book_cadence():
    c = m.book_cadence([0.0, 30.0, 60.0, 90.0], 0.0, 120.0)
    assert c["pushes"] == 4 and c["per_min"] == pytest.approx(2.0) and c["last_age_s"] == pytest.approx(30.0)
    assert m.book_cadence([], 0.0, 60.0)["last_age_s"] is None


def test_book_cadence_ignores_pushes_after_window_end():
    c = m.book_cadence([1.0, 2.0, 12.0], 0.0, 10.0)
    assert c["last_age_s"] == pytest.approx(8.0)  # the push at 12 is outside the window
    assert c["pushes"] == 2
    assert m.book_cadence([12.0], 0.0, 10.0)["last_age_s"] is None


def test_ib_request_duration_per_instrument():
    stock, _opt, q, b = _fixture()
    samples = [
        {"key": "SYN", "cycle": i, "t": 110.0 + i, "t_request": 110.0 + i - d, "bid": 10.0, "ask": 10.02}
        for i, d in enumerate([2.0, 2.0, 4.0, 2.0, 20.0])
    ]
    res = m.analyze([stock], [], q, b, samples, (100.0, 160.0))
    req = res["per"]["SYN"]["ib_req"]
    assert req["n"] == 5 and req["median"] == pytest.approx(2.0) and req["p90"] == pytest.approx(13.6)


def test_quota_units_from_snapshots():
    snaps = [
        {"label": "before", "n_ok": 0, "own_used": 0, "total_used": 40},
        {"label": "QUOTE stocks", "n_ok": 5, "own_used": 5, "total_used": 45},
        {"label": "ORDER_BOOK stocks", "n_ok": 5, "own_used": 10, "total_used": 50},
    ]
    q = m.quota_units(snaps)
    assert q[0]["own_per_instrument"] == 1.0 and q[1]["total_per_instrument"] == 1.0


def test_book_and_quote_row_fields_tolerate_quirks():
    book = m.book_row_fields({"Bid": [(10.0, 300, 2, {})], "Ask": []})
    assert book == {"bid": 10.0, "bid_volume": 300.0, "ask": None, "ask_volume": None}
    row = m.quote_row_fields({"last_price": 1.0, "delta": "N/A", "data_date": "N/A", "implied_volatility": [1]})
    assert (
        row["last_price"] == 1.0
        and row["delta"] is None
        and row["data_date"] is None
        and row["implied_volatility"] is None
    )


# --- markdown ------------------------------------------------------------


def test_render_markdown_sections_and_refusal():
    stock, opt, q, b = _fixture()
    samples = [
        {"key": "SYN", "cycle": 0, "t": 110.0, "bid": 10.01, "ask": 10.03},
        {
            "key": opt.key,
            "cycle": 0,
            "t": 120.0,
            "bid": 1.0,
            "ask": 1.1,
            "greeks": {"impliedVol": 0.25, "delta": 0.5, "gamma": 0.02, "vega": 0.1, "theta": -0.01},
        },
    ]
    res = m.analyze([stock, opt], [], q, b, samples, (100.0, 160.0))
    meta = {
        "date_et": "2026-10-26",
        "session": "rth",
        "minutes": 10,
        "ib_interval": 30,
        "ib_cycles": 1,
        "window_start": 100.0,
        "window_end": 160.0,
        "opend": "127.0.0.1:11111",
        "api_url": "http://127.0.0.1:8321",
        "jsonl": "output/research/x.jsonl",
        "verticals": [],
        "quota": m.quota_units(
            [
                {"label": "before", "own_used": 0, "total_used": 1},
                {"label": "QUOTE stocks", "n_ok": 1, "own_used": 1, "total_used": 2},
            ]
        ),
        "snapshots": [],
        "subscribe_results": [
            {"code": opt.futu_code, "subtype": "ORDER_BOOK", "ok": False, "error": "synthetic refusal text", "t": 0.0}
        ],
        "unsubscribe_results": [],
        "push_errors": 0,
    }
    md = m.render_markdown(meta, res)
    for needle in (
        "# Futu vs IB quotes calibration (2026-10-26, rth)",
        "## Quota units measured",
        "synthetic refusal text",
        "## Agreement at matched times (cents, Futu minus IB)",
        "within 1¢",
        "within 5¢",
        "policy tolerance, not the instrument's tick",
        "## IB request duration",
        "## Greek ratios",
        "## Decisions (lead fills)",
        "Every number below was observed in this run",
    ):
        assert needle in md
    assert "tick" not in md.replace("not the instrument's tick", "").lower().replace("ticker", "")
    assert "Overnight observations" not in md
    meta["session"] = "overnight"
    assert "## Overnight observations (stocks)" in m.render_markdown(meta, res)


def test_recorder_writes_jsonl(tmp_path):
    rec = m.Recorder(tmp_path / "r.jsonl")
    rec.quote_push(1.0, "US.SYN", {"last_price": 1.0})
    rec.book_push(2.0, "US.SYN", {"bid": 1.0})
    rec.close()
    kinds = [json.loads(line)["kind"] for line in (tmp_path / "r.jsonl").read_text().splitlines()]
    assert kinds == ["futu_quote", "futu_book"]
    assert len(rec.quotes["US.SYN"]) == 1 and len(rec.books["US.SYN"]) == 1


# --- offline re-render from a stored JSONL -------------------------------


def _write_synthetic_jsonl(path):
    """Synthetic run: one stock, a vertical, 2 IB cycles, window start/end NOT recorded."""
    lines = [
        {
            "kind": "meta",
            "ts": 1793024025.0,
            "session": "rth",
            "instruments": ["SYN"],
            "args": {
                "stocks": "SYN",
                "options": "",
                "vertical": [],
                "minutes": 1.0,
                "ib_interval": 30.0,
                "api_url": "http://127.0.0.1:8321",
                "opend_host": "h",
                "opend_port": 1,
                "session": "rth",
                "out_dir": "o",
                "doc_dir": "d",
                "dry_run": False,
            },
        },
        {
            "kind": "query_subscription",
            "ts": 1793024026.0,
            "label": "before",
            "n_ok": 0,
            "own_used": 0,
            "total_used": 5,
            "remain": 995,
        },
        {"kind": "subscribe", "ts": 1793024026.5, "code": "US.SYN", "subtype": "QUOTE", "ok": True, "error": None},
        {
            "kind": "query_subscription",
            "ts": 1793024027.0,
            "label": "QUOTE stocks",
            "n_ok": 1,
            "own_used": 1,
            "total_used": 6,
            "remain": 994,
        },
        {"kind": "futu_book", "ts": 1793024030.0, "code": "US.SYN", "bid": 10.00, "ask": 10.02},
        {"kind": "futu_book", "ts": 1793024090.0, "code": "US.SYN", "bid": 10.05, "ask": 10.07},
        {"kind": "futu_book", "ts": 1793024100.0, "code": "US.SYN", "bid": 99.0, "ask": 99.5},  # after the window
        {
            "kind": "ib_sample",
            "ts": 1793024032.0,
            "key": "SYN",
            "cycle": 0,
            "bid": 10.01,
            "ask": 10.03,
            "t_request": 1793024030.0,
        },
        {
            "kind": "ib_sample",
            "ts": 1793024062.0,
            "key": "SYN",
            "cycle": 1,
            "bid": 10.01,
            "ask": 10.03,
            "t_request": 1793024060.0,
        },
        {"kind": "unsubscribe", "ts": 1793024095.0, "code": "US.SYN", "subtype": "QUOTE", "error": None},
    ]
    path.write_text("\n".join(json.dumps(r) for r in lines) + "\n", encoding="utf-8")


def test_load_jsonl_derives_window_when_not_recorded(tmp_path):
    p = tmp_path / "run.jsonl"
    _write_synthetic_jsonl(p)
    run = m.load_jsonl(p)
    assert run.window == (1793024030.0, 1793024095.0)  # first IB request .. first unsubscribe record
    assert run.window_source == "derived"
    assert [i.key for i in run.instruments] == ["SYN"]
    assert len(run.ib_samples) == 2 and run.ib_samples[0]["t"] == 1793024032.0
    assert run.meta["ib_cycles"] == 2 and run.meta["date_et"] == "2026-10-26"
    assert run.meta["quota"][0]["own_per_instrument"] == 1.0
    # the book push at 100.0 is after window_end: not counted, not used as the end age
    res = m.analyze(run.instruments, run.verticals, run.quotes, run.books, run.ib_samples, run.window)
    cad = res["per"]["SYN"]["book_cadence"]
    assert cad["pushes"] == 2 and cad["last_age_s"] == pytest.approx(5.0)


def test_load_jsonl_prefers_recorded_window(tmp_path):
    p = tmp_path / "run.jsonl"
    _write_synthetic_jsonl(p)
    with p.open("a") as fh:
        fh.write(
            json.dumps(
                {"kind": "meta_window", "ts": 1793024094.0, "window_start": 1793024029.0, "window_end": 1793024094.0}
            )
            + "\n"
        )
    run = m.load_jsonl(p)
    assert run.window == (1793024029.0, 1793024094.0) and run.window_source == "recorded"


def test_from_jsonl_renders_offline(tmp_path, monkeypatch):
    p = tmp_path / "run.jsonl"
    _write_synthetic_jsonl(p)

    def _no_network(*a, **k):
        raise AssertionError("network used in --from-jsonl")

    monkeypatch.setattr(m.urllib.request, "urlopen", _no_network)
    monkeypatch.setattr(m.ApiClient, "call", _no_network)
    monkeypatch.delenv("XENON_QUERY_API_KEY", raising=False)
    rc = m.main(["--from-jsonl", str(p), "--doc-dir", str(tmp_path)])
    assert rc == 0
    md = (tmp_path / "futu-vs-ib-quotes-2026-10-26-rth.md").read_text(encoding="utf-8")
    assert "# Futu vs IB quotes calibration (2026-10-26, rth)" in md
    assert "derived from the raw samples" in md and f"`{p}`" in md
