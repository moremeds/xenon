from xenon.execution.ib_sync import _format_position_legs


def test_formatted_legs_include_conid():
    raw_legs = [
        {
            "conId": 756733,
            "right": "C",
            "strike": 500.0,
            "position": 1,
            "entry_cost": 250.0,
            "avgCost": 2.5,
            "marketPrice": 2.6,
            "marketValue": 260.0,
        }
    ]
    out = _format_position_legs(raw_legs)
    assert out[0]["conId"] == 756733
    assert out[0]["type"] == "Call"
