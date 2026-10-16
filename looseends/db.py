"""FalkorDB connection.

Uses a FalkorDB server when FALKORDB_HOST is set (Docker, self-hosted or
FalkorDB Cloud). Otherwise starts an embedded FalkorDB via falkordblite,
which bundles the same FalkorDB engine, so the project runs with no Docker.
"""
import os
from pathlib import Path

GRAPH_NAME = os.environ.get("LOOSEENDS_GRAPH", "looseends")
DEFAULT_DB_FILE = Path(__file__).resolve().parent.parent / "data" / "looseends.db"


def connect():
    host = os.environ.get("FALKORDB_HOST")
    if host:
        from falkordb import FalkorDB

        return FalkorDB(
            host=host,
            port=int(os.environ.get("FALKORDB_PORT", 6379)),
            username=os.environ.get("FALKORDB_USERNAME") or None,
            password=os.environ.get("FALKORDB_PASSWORD") or None,
        )
    from redislite.falkordb_client import FalkorDB

    DEFAULT_DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    return FalkorDB(str(DEFAULT_DB_FILE))


def graph(name: str = GRAPH_NAME):
    return connect().select_graph(name)


def rows(g, query: str, params: dict | None = None) -> list[dict]:
    """Run a query and return rows as dicts keyed by column name."""
    res = g.query(query, params or {})
    cols = [h[1] if isinstance(h, (list, tuple)) else h for h in res.header]
    return [dict(zip(cols, r)) for r in res.result_set]
