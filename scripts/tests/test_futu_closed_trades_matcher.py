"""FIFO lot-matcher: realized P&L, options multiplier, shorts, determinism, warnings."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from xenon.api.services.futu_closed_trades import (
    CORPORATE_ACTIONS,
    apply_corporate_actions,
    closed_lots_to_rows,
    match_closed_lots,
)


def _t(deal_id, side, qty, price, day, code="US.QQQ", ticker="QQQ", order_id=None):
    return {
        "futu_deal_id": deal_id,
        "futu_order_id": order_id if order_id is not None else f"ord-{deal_id}",
        "futu_code": code,
        "ticker": ticker,
        "quantity": qty,
        "price": price,
        "filled_at": datetime(2026, 6, day, tzinfo=timezone.utc),
        "raw": {"trd_side": side},
    }


def test_long_round_trip_realized_pnl():
    lots = match_closed_lots([_t("d1", "BUY", 10, 100, 1), _t("d2", "SELL", 10, 110, 2)])
    assert len(lots) == 1
    lot = lots[0]
    assert lot.action == "SELL"
    assert lot.quantity == Decimal("10")
    assert lot.realized_pnl == Decimal("100")  # (110-100)*10*1
    assert lot.cost_basis == Decimal("1000")
    assert lot.proceeds == Decimal("1100")
    assert lot.opened_at.day == 1 and lot.closed_at.day == 2
    assert lot.futu_close_id == "d2:d1"


def test_option_multiplier_applied():
    code, tk = "US.QQQ250620C500000", "QQQ250620C500000"
    lots = match_closed_lots([_t("d1", "BUY", 1, "3.48", 1, code, tk), _t("d2", "SELL", 1, "10.40", 2, code, tk)])
    assert lots[0].realized_pnl == Decimal("692.00")  # (10.40-3.48)*1*100


def test_short_round_trip():
    lots = match_closed_lots([_t("d1", "SELL_SHORT", 5, 50, 1), _t("d2", "BUY_BACK", 5, 40, 2)])
    assert lots[0].action == "BUY"
    assert lots[0].realized_pnl == Decimal("50")  # (50-40)*5
    assert lots[0].futu_close_id == "d2:d1"


def test_partial_close_across_two_open_lots_unique_ids():
    lots = match_closed_lots([_t("b1", "BUY", 5, 100, 1), _t("b2", "BUY", 5, 100, 1), _t("s1", "SELL", 10, 110, 2)])
    assert {l.futu_close_id for l in lots} == {"s1:b1", "s1:b2"}
    assert sum(l.realized_pnl for l in lots) == Decimal("100")  # (110-100)*10


def test_close_with_no_open_lot_warns_once_per_deal_and_emits_nothing(caplog):
    """The rebuild reruns on every sync: WARNING the first time, DEBUG after."""
    import logging

    from xenon.api.services import futu_closed_trades as mod

    mod._UNMATCHED_CLOSE_WARNED.clear()
    trades = [_t("s1", "SELL", 5, 100, 1)]
    with caplog.at_level(logging.DEBUG, logger="xenon.api.services.futu_closed_trades"):
        assert match_closed_lots(trades) == []
        assert match_closed_lots(trades) == []
        assert match_closed_lots([_t("s2", "SELL", 5, 100, 2)]) == []
    levels = [r.levelno for r in caplog.records if "no open lot" in r.getMessage()]
    assert levels == [logging.WARNING, logging.DEBUG, logging.WARNING]


def test_unknown_side_skipped(caplog):
    import logging

    with caplog.at_level(logging.WARNING):
        lots = match_closed_lots([_t("x1", "WAT", 1, 1, 1)])
    assert lots == []
    assert any("unknown trd_side" in r.message for r in caplog.records)


def test_close_ids_stable_regardless_of_input_order():
    """Same-timestamp fills must produce identical close ids on a re-pull (shuffle-invariant)."""
    same_day = [
        _t("b1", "BUY", 5, 100, 1),
        _t("b2", "BUY", 5, 100, 1),
        _t("s1", "SELL", 10, 110, 1),
    ]
    ids_forward = {l.futu_close_id for l in match_closed_lots(same_day)}
    ids_reversed = {l.futu_close_id for l in match_closed_lots(list(reversed(same_day)))}
    assert ids_forward == ids_reversed


def test_closed_lots_to_rows_shape():
    lots = match_closed_lots([_t("d1", "BUY", 1, 100, 1), _t("d2", "SELL", 1, 150, 2)])
    rows = closed_lots_to_rows(lots)
    assert rows[0]["futu_close_id"] == "d2:d1"
    assert rows[0]["realized_pnl"] == Decimal("50")
    assert set(rows[0]) >= {"ticker", "action", "quantity", "cost_basis", "proceeds", "opened_at", "closed_at"}


def test_close_order_id_captured_from_closing_fill():
    """The closing fill's order id is carried on every lot it produced — so the
    HISTORICAL surface can fuse legs of one multi-leg close into a structure."""
    lots = match_closed_lots(
        [
            _t("o1", "BUY", 1, 100, 1, order_id="OPEN-A"),
            _t("c1", "SELL", 1, 150, 2, order_id="CLOSE-Z"),
        ]
    )
    assert lots[0].close_order_id == "CLOSE-Z"  # closing order, not the open
    assert closed_lots_to_rows(lots)[0]["metadata"]["close_order_id"] == "CLOSE-Z"


def test_multi_leg_close_shares_one_close_order_id():
    """Two legs closed by ONE order (Futu places structures as a single order)
    share a close_order_id even though their deal ids differ."""
    cc = "US.AAOI270115C200000"
    cp = "US.AAOI270115C190000"
    lots = match_closed_lots(
        [
            _t("oa", "SELL_SHORT", 10, 54.0, 1, code=cc, ticker="AAOI270115C200000", order_id="O-OPEN"),
            _t("ob", "BUY", 10, 56.0, 1, code=cp, ticker="AAOI270115C190000", order_id="O-OPEN"),
            _t("ca", "BUY_BACK", 10, 54.07, 2, code=cc, ticker="AAOI270115C200000", order_id="O-CLOSE"),
            _t("cb", "SELL", 10, 56.17, 2, code=cp, ticker="AAOI270115C190000", order_id="O-CLOSE"),
        ]
    )
    assert {l.close_order_id for l in lots} == {"O-CLOSE"}
    assert {l.ticker for l in lots} == {"AAOI270115C200000", "AAOI270115C190000"}


# --- Corporate actions -------------------------------------------------------
#
# The deal rows below are the real futu_trades rows for the affected codes, as
# stored in prod and labelled as-of 2026-10-22 (source: CSV export in
# tmp/repo-review/work/futu_trades.csv; expected P&L per
# tmp/repo-review/evidence/futu-closed-trades-unmatched.md §1.3/§3).


def _real(deal_id, order_id, code, ticker, side, qty, price, ts):
    """`ts` is the export's filled_at string verbatim, including its +08 offset;
    astimezone(utc) recovers the stored timestamptz instant."""
    return {
        "futu_deal_id": deal_id,
        "futu_order_id": order_id,
        "futu_code": code,
        "ticker": ticker,
        "quantity": qty,
        "price": price,
        "filled_at": datetime.fromisoformat(ts).astimezone(timezone.utc),
        "raw": {"trd_side": side},
    }


# KLAC 1:10 split ex 2026-06-12.
KLAC_DEALS = [
    _real("551721076191430204", "FH1CA8A183F6648000", "US.KLAC", "KLAC", "BUY", "2", "2337.87", "2026-06-11 18:25:49.415+08"),
    _real("1538859511105244753", "FH1CAA0F8A65A48000", "US.KLAC", "KLAC", "SELL", "5", "250.89", "2026-06-12 21:05:03.822+08"),
    _real("783616101456910479", "FH1CADC265ED3F2000", "US.KLAC", "KLAC", "SELL", "10", "257.35", "2026-06-15 18:01:46.257+08"),
    _real("5419113579080550585", "FH1CC3B7F3D85FA000", "US.KLAC", "KLAC", "SELL", "5", "241.255", "2026-07-02 19:22:43.082+08"),
]

# MULL 1:25 split ex 2026-06-26 (all MULL deals in the export).
MULL_DEALS = [
    _real("7889600215611417687", "FH1C6857B3177F2000", "US.MULL", "MULL", "BUY", "20", "204.85", "2026-04-22 20:00:24.329+08"),
    _real("2137672290744356444", "FH1C6988AEBCAE8000", "US.MULL", "MULL", "SELL", "10", "202.41", "2026-04-23 18:12:59.194+08"),
    _real("586122196613451693", "FH1C69ADE949FF2000", "US.MULL", "MULL", "SELL", "10", "216.71", "2026-04-23 20:55:33.22+08"),
    _real("4279150322247044323", "FH1C72A0714C248000", "US.MULL", "MULL", "BUY", "20", "231.69", "2026-04-30 19:43:02.24+08"),
    _real("8735509472060483224", "FH1C77C95012DFA000", "US.MULL", "MULL", "SELL", "7", "299.72", "2026-05-04 19:55:02.378+08"),
    _real("2039049044182875543", "FH1C7A404F432E8000", "US.MULL", "MULL", "SELL", "3", "371.99", "2026-05-06 17:51:06.076+08"),
    _real("3054720353001769873", "FH1C7A43042CFF2000", "US.MULL", "MULL", "SELL", "2", "362.99", "2026-05-06 18:02:55.472+08"),
    _real("3054720383066540938", "FH1C7A43042CFF2000", "US.MULL", "MULL", "SELL", "3", "363.53", "2026-05-06 18:02:55.485+08"),
    _real("4414874537889063263", "FH1C8153CD66248000", "US.MULL", "MULL", "SELL", "4", "499.46", "2026-05-12 05:45:24.255+08"),
    _real("5416079178695708844", "FH1CC3D8CE5D6E8000", "US.MULL", "MULL", "SELL", "25", "24.9", "2026-07-02 21:54:03.339+08"),
]

# MUU 1:20 split ex 2026-07-15 (all MUU deals in the export).
MUU_DEALS = [
    _real("5994562117398573069", "FH1BBC768B4C9FA000", "US.MUU", "MUU", "BUY", "40", "81.8", "2025-12-10 06:58:44.288+08"),
    _real("9087916596209143592", "FH1BBC768B4C9FA000", "US.MUU", "MUU", "BUY", "30", "81.8", "2025-12-10 06:58:46.724+08"),
    _real("5739341797405140633", "FH1BBFB8C1BA3F2000", "US.MUU", "MUU", "SELL", "70", "73.7732", "2025-12-12 19:41:09.373+08"),
    _real("3076965221687518482", "FH1BBFC682033F2000", "US.MUU", "MUU", "BUY", "100", "76.3731", "2025-12-12 20:41:14.129+08"),
    _real("2847932799449623117", "FH1BC7563284E48000", "US.MUU", "MUU", "SELL", "50", "83.22", "2025-12-18 17:40:38.366+08"),
    _real("4021342825596508467", "FH1BE26E8912BF2000", "US.MUU", "MUU", "SELL", "35", "127.61", "2026-01-08 18:43:42.856+08"),
    _real("3639763752441396800", "FH1BF33F8FEC648000", "US.MUU", "MUU", "SELL", "14", "176.42", "2026-01-21 20:15:08.076+08"),
    _real("5942669653727522203", "FH1C01FA826EDFA000", "US.MUU", "MUU", "SELL", "1", "183.94", "2026-02-02 06:48:11.467+08"),
    _real("4415402038682198034", "FH1CCA24545FFF2000", "US.MUU", "MUU", "BUY", "10", "652.5518", "2026-07-07 19:07:06.827+08"),
    _real("8520748158023696726", "FH1CCB712B73A48000", "US.MUU", "MUU", "SELL", "10", "661.19", "2026-07-08 19:21:19.547+08"),
    _real("775148406064838476", "FH1CCB7F66A9AE8000", "US.MUU", "MUU", "BUY", "10", "688.4", "2026-07-08 20:23:38.963+08"),
    _real("6291602740894447974", "FH1CCCA0897FDFA000", "US.MUU", "MUU", "SELL", "3", "801.44", "2026-07-09 17:26:44.732+08"),
    _real("6294134316770660165", "FH1CDEAB60C1648000", "US.MUU", "MUU", "SELL", "140", "37.04", "2026-07-23 17:46:46.129+08"),
]

# RKLX 1:3 split ex 2025-12-09 (all RKLX deals in the export).
RKLX_DEALS = [
    _real("433789521894061553", "FH1BB4BA64387F2000", "US.RKLX", "RKLX", "BUY", "100", "65.3", "2025-12-04 06:48:48.897+08"),
    _real("5141367076140530346", "FH1BBA899E86EE8000", "US.RKLX", "RKLX", "SELL", "50", "75.84", "2025-12-08 19:03:15.483+08"),
    _real("6268673564604040409", "FH1BC37BA6DA248000", "US.RKLX", "RKLX", "SELL", "117", "36.5529", "2025-12-15 17:48:06.33+08"),
    _real("736377355881651332", "FH1BC37BA6DA248000", "US.RKLX", "RKLX", "SELL", "33", "36.5529", "2025-12-15 17:48:06.52+08"),
    _real("5125808864660867364", "FH1C7A082B27E48000", "US.RKLX", "RKLX", "BUY", "100", "36.76", "2026-05-06 13:45:35.412+08"),
    _real("8194412516201329905", "FH1C7B8F95ADAE8000", "US.RKLX", "RKLX", "SELL", "30", "39.28", "2026-05-07 18:15:44.148+08"),
    _real("1674066080547058390", "FH1C7B8F95ADAE8000", "US.RKLX", "RKLX", "SELL", "43", "39.28", "2026-05-07 18:16:04.073+08"),
    _real("1674066097726927570", "FH1C7B8F95ADAE8000", "US.RKLX", "RKLX", "SELL", "12", "39.28", "2026-05-07 18:16:04.085+08"),
    _real("6944625592538020081", "FH1C7CEAC3761FA000", "US.RKLX", "RKLX", "SELL", "15", "53.46", "2026-05-08 19:32:33.534+08"),
    _real("346419894386541230", "FH1CC3BAE7F79FA000", "US.RKLX", "RKLX", "BUY", "150", "44.42", "2026-07-02 19:35:45.464+08"),
    _real("4640565813328218652", "FH1CCA182430EE8000", "US.RKLX", "RKLX", "SELL", "150", "30.08", "2026-07-07 18:13:51.714+08"),
]

# LCID 10:1 reverse split ex 2025-09-02 — options re-coded LCID… -> LCID1….
LCID_DEALS = [
    _real("3567647905516575515", "FH1B3484A4D42E8000", "US.LCID250919C3000", "LCID250919C3000", "BUY", "1", "0.04", "2025-08-26 17:44:36.058+08"),
    _real("5930076112806275811", "FH1B3484A4D42E8000", "US.LCID250919C3000", "LCID250919C3000", "BUY", "6", "0.04", "2025-08-26 17:44:36.079+08"),
    _real("1534306543998631181", "FH1B3484A4D42E8000", "US.LCID250919C3000", "LCID250919C3000", "BUY", "157", "0.04", "2025-08-26 17:44:36.1+08"),
    _real("5582621372204217855", "FH1B3484A4D42E8000", "US.LCID250919C3000", "LCID250919C3000", "BUY", "214", "0.04", "2025-08-26 17:44:36.122+08"),
    _real("592709540240397533", "FH1B3484A4D42E8000", "US.LCID250919C3000", "LCID250919C3000", "BUY", "22", "0.04", "2025-08-26 17:44:36.144+08"),
    _real("4725712047428184090", "FH1B3ED2854A5B6000", "US.LCID1250919C3000", "LCID1250919C3000", "SELL", "1", "0.04", "2025-09-03 18:12:17.736+08"),
    _real("5113277446254875316", "FH1B5689F8CCAE8000", "US.LCID1250919C3000", "LCID1250919C3000", "SELL", "399", "0", "2025-09-22 03:56:13.465+08"),
]

# ZIM $0.84 special dividend ex 2024-12-02 — option strikes reduced by $0.84
# (P19000->P18160, P20000->P19160).
ZIM_DEALS = [
    _real("5475324579741259451", "FH19D6629C8F5B6000", "US.ZIM241220P19000", "ZIM241220P19000", "BUY", "50", "1.64", "2024-11-27 17:50:38.334+08"),
    _real("629569840648324420", "FH19D6629C8F5B6000", "US.ZIM241220P20000", "ZIM241220P20000", "SELL_SHORT", "50", "2.17", "2024-11-27 17:50:38.334+08"),
    _real("1443246001975349768", "FH19DCD0D3B61B6000", "US.ZIM241220P18160", "ZIM241220P18160", "SELL", "25", "0.73", "2024-12-02 17:39:58.102+08"),
    _real("814914156992236583", "FH19DCD0D3B61B6000", "US.ZIM241220P18160", "ZIM241220P18160", "SELL", "25", "0.73", "2024-12-02 17:39:58.13+08"),
    _real("2264440100446873163", "FH19DCD0D3B61B6000", "US.ZIM241220P19160", "ZIM241220P19160", "BUY_BACK", "25", "1.16", "2024-12-02 17:39:58.102+08"),
    _real("7133257393165252392", "FH19DCD0D3B61B6000", "US.ZIM241220P19160", "ZIM241220P19160", "BUY_BACK", "25", "1.16", "2024-12-02 17:39:58.13+08"),
]


def test_split_klac_close_matches_and_pnl():
    """1:10 split ex 2026-06-12: BUY 2 @2337.87 becomes 20 @233.787; the three
    post-split closes all match. Evidence §3: stored -4173.96 -> +358.49."""
    lots = match_closed_lots(KLAC_DEALS, actions=CORPORATE_ACTIONS)
    assert [l.quantity for l in lots] == [Decimal("5"), Decimal("10"), Decimal("5")]
    assert {l.futu_code for l in lots} == {"US.KLAC"}
    assert sum(l.realized_pnl for l in lots) == Decimal("358.485")


def test_split_mull_whole_book_pnl():
    """1:25 split ex 2026-06-26: 1 pre-split share remains -> 25 post-split units;
    SELL 25 @24.90 fully matches. Evidence §3: whole-book +3111.32."""
    lots = match_closed_lots(MULL_DEALS, actions=CORPORATE_ACTIONS)
    # Matched qty is in post-split units: 39 pre-split shares as 39x25 + the final 25.
    assert sum(l.quantity for l in lots) == Decimal("1000")
    assert sum(l.realized_pnl for l in lots) == Decimal("3111.32")


def test_split_muu_whole_book_pnl():
    """1:20 split ex 2026-07-15: 7 pre-split shares remain -> 140 post-split
    units; SELL 140 @37.04 fully matches. Evidence §3: whole-book +3874.29."""
    lots = match_closed_lots(MUU_DEALS, actions=CORPORATE_ACTIONS)
    # Matched qty is in post-split units: 183 pre-split shares as 183x20 + the final 140.
    assert sum(l.quantity for l in lots) == Decimal("3800")
    assert sum(l.realized_pnl for l in lots) == Decimal("3874.286")


def test_split_rklx_whole_book_pnl():
    """1:3 split ex 2025-12-09: 50 pre-split shares -> 150 units; SELL 117+33
    fully match. Evidence §3: whole-book +1058.64 (divisor 3 -> cents tolerance)."""
    lots = match_closed_lots(RKLX_DEALS, actions=CORPORATE_ACTIONS)
    assert abs(sum(l.realized_pnl for l in lots) - Decimal("1058.635")) < Decimal("0.005")


def test_rename_lcid_options_recoded_to_lcid1():
    """LCID 10:1 reverse split ex 2025-09-02 re-codes options LCID… -> LCID1…;
    the 400 contracts bought pre-split match the post-split SELLs. -1596.00."""
    lots = match_closed_lots(LCID_DEALS, actions=CORPORATE_ACTIONS)
    assert {l.futu_code for l in lots} == {"US.LCID1250919C3000"}
    assert sum(l.quantity for l in lots) == Decimal("400")
    assert sum(l.realized_pnl for l in lots) == Decimal("-1596")


def test_rename_zim_strike_adjust_put_spread():
    """ZIM $0.84 special dividend cut both put strikes by $0.84; the spread closes
    under the adjusted codes: long leg -4550.00 + short leg +5050.00 = +500.00."""
    lots = match_closed_lots(ZIM_DEALS, actions=CORPORATE_ACTIONS)
    long_pnl = sum(l.realized_pnl for l in lots if l.futu_code == "US.ZIM241220P18160")
    short_pnl = sum(l.realized_pnl for l in lots if l.futu_code == "US.ZIM241220P19160")
    assert long_pnl == Decimal("-4550")
    assert short_pnl == Decimal("5050")
    assert sum(l.realized_pnl for l in lots) == Decimal("500")


def test_split_boundary_post_market_fill_is_pre_ex():
    """SIMULATED boundary (not an observed fill — Astra P2): a US post-market
    fill at 19:30 ET on the day before the ex-date is stored as 19:30 on D-1
    (ET wall labelled UTC; the export would render it 2025-12-09 03:30+08).
    Trade date = D-1 -> pre-ex -> the lot is split-adjusted and both post-ex
    closes match fully (+2217.935); 150 units stay open."""
    buy = _real("SIM-B1", "O-SIM", "US.RKLX", "RKLX", "BUY", "100", "65.3", "2025-12-09 03:30:00+08")
    assert buy["filled_at"] == datetime(2025, 12, 8, 19, 30, tzinfo=timezone.utc)  # stored instant
    adj = apply_corporate_actions([buy], CORPORATE_ACTIONS)
    assert adj[0]["quantity"] == Decimal("300")  # 1:3 split ex 2025-12-09
    sells = [RKLX_DEALS[2], RKLX_DEALS[3]]  # the two real 2025-12-15 SELLs
    lots = match_closed_lots([buy, *sells], actions=CORPORATE_ACTIONS)
    assert sum(l.quantity for l in lots) == Decimal("150")
    assert abs(sum(l.realized_pnl for l in lots) - Decimal("2217.935")) < Decimal("0.005")


def test_split_boundary_ex_date_regular_fill_not_adjusted():
    """SIMULATED boundary: a regular-session fill at 09:45 ET on the ex-date
    itself (stored 09:45 on D, export 17:45+08) is post-split -> untouched."""
    buy = _real("SIM-B2", "O-SIM", "US.RKLX", "RKLX", "BUY", "100", "36.5", "2025-12-09 17:45:00+08")
    assert buy["filled_at"] == datetime(2025, 12, 9, 9, 45, tzinfo=timezone.utc)
    adj = apply_corporate_actions([buy], CORPORATE_ACTIONS)
    assert adj[0] is buy  # post-ex rows pass through as the same object


def test_no_actions_behaviour_unchanged(caplog):
    """With no actions the matcher behaves exactly as before: pre-split opens do
    not match post-split closes, so KLAC warns and stores -4173.96."""
    import logging

    from xenon.api.services import futu_closed_trades as mod

    mod._UNMATCHED_CLOSE_WARNED.clear()
    with caplog.at_level(logging.WARNING, logger="xenon.api.services.futu_closed_trades"):
        lots = match_closed_lots(KLAC_DEALS)
    assert sum(l.realized_pnl for l in lots) == Decimal("-4173.96")  # 2 units @2337.87 vs 5 @250.89
    unmatched_warns = [r for r in caplog.records if "no open lot" in r.getMessage()]
    assert len(unmatched_warns) == 3  # 3 + 10 + 5 post-split units unmatched


def test_apply_corporate_actions_is_pure_and_empty_is_noop():
    """The pre-pass never mutates its input; an empty action list is a no-op."""
    snapshot = [dict(d) for d in KLAC_DEALS]
    assert apply_corporate_actions(KLAC_DEALS, ()) == KLAC_DEALS
    out = apply_corporate_actions(KLAC_DEALS, CORPORATE_ACTIONS)
    assert KLAC_DEALS == snapshot  # input untouched
    buy = out[0]  # pre-split BUY 2 @2337.87 -> 20 @233.787, total cost unchanged
    assert buy["quantity"] == Decimal("20")
    assert buy["price"] == Decimal("233.787")
    assert buy["quantity"] * buy["price"] == Decimal("2") * Decimal("2337.87")
    assert out[1] is KLAC_DEALS[1]  # post-split rows pass through untouched

