from xenon.api.server import app


def test_uw_analyze_fastapi_routes_are_removed():
    paths = {route.path for route in app.routes}

    assert "/uw-analyze" not in paths
    assert "/uw-analyze/portfolio" not in paths
    assert "/uw-analyze/refresh" not in paths
