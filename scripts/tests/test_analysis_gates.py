from xenon.analysis.gates import earnings_gate, liquidity_gate, regime_gate


def test_earnings_gate_blocks_within_window():
    assert earnings_gate(earnings_within_14d=True, window_days=14) is False
    assert earnings_gate(earnings_within_14d=False, window_days=14) is True


def test_earnings_gate_respects_custom_window():
    assert earnings_gate(earnings_within_14d=True, window_days=2) is False


def test_liquidity_gate_requires_min_option_volume():
    assert liquidity_gate(option_volume=500, min_volume=1000) is False
    assert liquidity_gate(option_volume=1500, min_volume=1000) is True
    assert liquidity_gate(option_volume=None, min_volume=1000) is False


def test_regime_gate_blocks_r2_only():
    assert regime_gate(regime="R0") is True
    assert regime_gate(regime="R1") is True
    assert regime_gate(regime="R2") is False
