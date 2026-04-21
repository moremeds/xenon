from xenon.execution.preflight import ReasonCode


def test_new_reason_codes_present():
    names = {m.name for m in ReasonCode}
    assert "STALE_QUOTE" in names
    assert "LIMIT_OUT_OF_BAND" in names
    assert "LIMIT_OFF_TICK" in names
    assert "ATTEMPT_ID_TERMINAL" in names
    # F5 — cancel/modify failure classification
    assert "IB_CONNECTION" in names
    assert "OWNERSHIP" in names
    assert "IB_REJECT" in names
    assert "MODIFY_STALE" in names
    assert "MODIFY_SEQUENCE_REQUIRED" in names
    assert "ORDER_NOT_FOUND" in names
