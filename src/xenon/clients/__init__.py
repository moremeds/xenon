"""API client modules for external data sources."""

__all__ = ["IBClient", "UWClient", "FutuClient"]


def __getattr__(name):
    if name == "IBClient":
        from xenon.clients.ib_client import IBClient

        return IBClient
    if name == "UWClient":
        from xenon.clients.uw_client import UWClient

        return UWClient
    if name == "FutuClient":
        from xenon.clients.futu_client import FutuClient

        return FutuClient
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
