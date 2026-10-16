"""FalkorDB connection helpers (one server, one graph per tenant, added in Phase 1)."""
from falkordb import FalkorDB

from app.config import get_settings


def get_client() -> FalkorDB:
    s = get_settings()
    return FalkorDB(
        host=s.falkordb_host,
        port=s.falkordb_port,
        password=s.falkordb_password or None,
        socket_connect_timeout=2,
    )


def check_falkordb() -> dict:
    """Ping the server and list existing graphs. Never raises; never echoes credentials."""
    try:
        client = get_client()
        client.connection.ping()
        return {"reachable": True, "graphs": sorted(client.list_graphs()), "error": None}
    except Exception as exc:  # connection refused, timeout, auth failure, ...
        return {"reachable": False, "graphs": [], "error": type(exc).__name__}
