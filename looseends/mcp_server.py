"""LooseEnds as an MCP server, so any MCP client (Cursor, VS Code, other agents) can
check the company graph before acting.

Run over stdio:
    python -m looseends.mcp_server

The web app also serves it over streamable HTTP at /mcp.
"""
import json

from mcp.server.mcpserver import MCPServer

from . import tools as T
from .db import graph

mcp = MCPServer(
    name="looseends",
    instructions=(
        "LooseEnds finds knowledge that has quietly died in an organisation's decision graph (Kubernetes KEPs): "
        "open decisions resting on retired ones, missed release promises, and work whose owners went quiet. "
        "Call impact_of before changing a decision, who_knows to find an owner, in_force to check a decision "
        "is still current. Suggested links are unconfirmed inferences."),
)


def _out(result) -> str:
    return json.dumps(result, default=str, indent=1)


@mcp.tool(description="Run the loose-end detectors. kind: all, stale, broken_promises or orphans.")
def scan_loose_ends(kind: str = "all", limit: int = 10) -> str:
    return _out(T.scan(graph(), kind, limit))


@mcp.tool(description="What breaks if a decision (KEP number) changes: affected open decisions, paths, owners.")
def impact_of(kep: int) -> str:
    return _out(T.ripple(graph(), kep))


@mcp.tool(description="Rank active people who could take over a decision (KEP number), with reasons.")
def who_knows(kep: int) -> str:
    return _out(T.who_can_take_over(graph(), kep))


@mcp.tool(description="Which decision is in force today, following replacements from the given KEP.")
def in_force(kep: int) -> str:
    return _out(T.in_force(graph(), kep))


@mcp.tool(description="Details of one decision, including any loose ends it is involved in.")
def get_decision(kep: int) -> str:
    return _out(T.get_decision(graph(), kep))


@mcp.tool(description="Full-text search over decision titles and summaries.")
def search_decisions(query: str) -> str:
    return _out(T.search_decisions(graph(), query))


@mcp.tool(description="Save a DRAFT follow-up asking a person to fix or confirm a loose end. A human approves it.")
def draft_followup(kep: int, assignee: str, reason: str) -> str:
    return _out(T.draft_followup(graph(), kep, assignee, reason))


@mcp.tool(description="Run a read-only Cypher query against the LooseEnds graph.")
def read_cypher(query: str) -> str:
    return _out(T.read_cypher(graph(), query))


if __name__ == "__main__":
    mcp.run("stdio")
