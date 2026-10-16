"""The graph tools the agent (and the MCP server) can call.

Each tool runs Cypher against FalkorDB and returns compact, JSON-safe data
with the evidence that backs it.
"""
import re
from datetime import datetime, timezone

from . import detectors as D
from .db import rows

WRITE_WORDS = re.compile(r"\b(CREATE|MERGE|SET|DELETE|REMOVE|DROP|LOAD|CALL\s+db\.idx\.fulltext\.(create|drop))\b", re.I)


def _short(items, limit):
    return {"total": len(items), "items": items[:limit]}


def scan(g, kind: str = "all", limit: int = 10) -> dict:
    kinds = {"stale": D.stale_citations, "broken_promises": D.broken_promises, "orphans": D.orphans}
    if kind == "all":
        return {"current_release": f"v1.{D.current_release(g)}",
                **{k: _short(f(g), limit) for k, f in kinds.items()}}
    if kind not in kinds:
        raise ValueError(f"kind must be one of: all, {', '.join(kinds)}")
    return {"current_release": f"v1.{D.current_release(g)}", kind: _short(kinds[kind](g), limit)}


def ripple(g, kep: int) -> dict:
    r = D.ripple(g, int(kep))
    return {"decision": r["decision"],
            "affected_open": [x for x in r["affected"] if x["open"]],
            "affected_closed": [x["number"] for x in r["affected"] if not x["open"]],
            "shares_feature_gate": r["shared_gate"],
            "suggested_links_unconfirmed": r["suggested"]}


def who_can_take_over(g, kep: int) -> dict:
    return {"kep": int(kep), "candidates": D.next_experts(g, int(kep))}


def in_force(g, kep: int) -> dict:
    return D.current_version(g, int(kep))


def get_decision(g, kep: int) -> dict:
    found = rows(g, """
        MATCH (d:Decision {number: $n})-[:OWNED_BY]->(t:Team)
        OPTIONAL MATCH (a:Person)-[:AUTHORED]->(d)
        OPTIONAL MATCH (d)-[:GATED_BY]->(f:FeatureGate)
        RETURN d.number AS number, d.title AS title, d.status AS status, d.stage AS stage,
               d.active AS open, d.retired AS retired, d.latest AS last_release, d.url AS url,
               d.summary AS summary, t.name AS team,
               collect(DISTINCT a.handle) AS authors, collect(DISTINCT f.name) AS feature_gates""",
                 {"n": int(kep)})
    if not found:
        raise ValueError(f"KEP-{kep} not found")
    d = found[0]
    d["depends_on"] = [r["n"] for r in rows(g, "MATCH (:Decision {number: $n})-[:DEPENDS_ON]->(x) RETURN x.number AS n",
                                            {"n": int(kep)})]
    d["depended_on_by"] = [r["n"] for r in rows(g, "MATCH (x)-[:DEPENDS_ON]->(:Decision {number: $n}) RETURN x.number AS n",
                                                {"n": int(kep)})]
    d["replaced_by"] = [r["n"] for r in rows(g, "MATCH (x)-[:REPLACES]->(:Decision {number: $n}) RETURN x.number AS n",
                                             {"n": int(kep)})]
    d["loose_ends"] = D.flags_for(g, int(kep))
    d["past_questions"] = rows(g, """
        MATCH (q:AgentRun)-[:ABOUT]->(:Decision {number: $n})
        RETURN q.question AS question, q.at AS at ORDER BY q.at DESC LIMIT 3""", {"n": int(kep)})
    d["open_followups"] = rows(g, """
        MATCH (f:FollowUp)-[:ABOUT]->(:Decision {number: $n}) WHERE f.status IN ['draft', 'approved']
        MATCH (f)-[:ASSIGNED_TO]->(p:Person)
        RETURN f.id AS id, f.status AS status, p.handle AS assignee, f.reason AS reason""", {"n": int(kep)})
    return d


STOPWORDS = set("""a about an and any anything are as at be by can do does for from has have how i in is it
kep keps me of on or our show tell that the this to was we what when where which who why with you""".split())


def search_decisions(g, query: str, limit: int = 8) -> dict:
    words = [w for w in re.findall(r"[A-Za-z0-9]+", query.lower()) if w not in STOPWORDS and len(w) > 1]
    if not words:
        return {"query": query, "results": []}
    found = rows(g, """
        CALL db.idx.fulltext.queryNodes('Decision', $q) YIELD node, score
        RETURN node.number AS number, node.title AS title, node.status AS status, score
        ORDER BY score DESC LIMIT $limit""", {"q": "|".join(words), "limit": int(limit)})
    return {"query": query, "results": found}


def person(g, handle: str) -> dict:
    h = handle.lstrip("@").lower()
    found = rows(g, "MATCH (p:Person {handle: $h}) RETURN p.handle AS handle, p.last_active AS last_active", {"h": h})
    if not found:
        raise ValueError(f"@{h} not found")
    p = found[0]
    p["last_active"] = f"v1.{p['last_active']}" if p["last_active"] is not None else None
    for role in ("AUTHORED", "REVIEWED", "APPROVED"):
        p[role.lower()] = rows(g, f"""
            MATCH (:Person {{handle: $h}})-[:{role}]->(d:Decision)
            RETURN d.number AS number, d.title AS title, d.status AS status
            ORDER BY d.latest DESC LIMIT 8""", {"h": h})
    p["teams"] = [r["t"] for r in rows(g, """
        MATCH (:Person {handle: $h})-[:AUTHORED|APPROVED]->(:Decision)-[:OWNED_BY]->(t:Team)
        RETURN t.name AS t, count(*) AS c ORDER BY c DESC LIMIT 3""", {"h": h})]
    return p


def draft_followup(g, kep: int, assignee: str, reason: str) -> dict:
    """Create a draft follow-up task in the graph. A human approves it before anything is sent."""
    h = assignee.lstrip("@").lower()
    ok = rows(g, "MATCH (d:Decision {number: $n}), (p:Person {handle: $h}) RETURN d.title AS title",
              {"n": int(kep), "h": h})
    if not ok:
        raise ValueError(f"KEP-{kep} or @{h} not found")
    fid = rows(g, "MATCH (f:FollowUp) RETURN count(f) + 1 AS n")[0]["n"]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = (f"Hi @{h}, LooseEnds flagged KEP-{kep} ({ok[0]['title']}).\n\n"
            f"Why: {reason}\n\nCould you confirm whether it needs an update, or point to who should own it? "
            f"Evidence is attached in the LooseEnds report.")
    rows(g, """
        MATCH (d:Decision {number: $n}), (p:Person {handle: $h})
        CREATE (f:FollowUp {id: $id, status: 'draft', reason: $reason, message: $body, created: $now})
        CREATE (f)-[:ABOUT]->(d), (f)-[:ASSIGNED_TO]->(p)""",
         {"n": int(kep), "h": h, "id": f"FU-{fid}", "reason": reason, "body": body, "now": now})
    return {"id": f"FU-{fid}", "status": "draft", "kep": int(kep), "assignee": h, "message": body,
            "note": "Draft saved in the graph. A person must approve it before it is sent."}


def list_followups(g, status: str = "all") -> dict:
    where = "" if status == "all" else "WHERE f.status = $s"
    return {"followups": rows(g, f"""
        MATCH (f:FollowUp)-[:ABOUT]->(d:Decision) {where}
        MATCH (f)-[:ASSIGNED_TO]->(p:Person)
        RETURN f.id AS id, f.status AS status, d.number AS kep, d.title AS title,
               p.handle AS assignee, f.reason AS reason, f.message AS message, f.created AS created
        ORDER BY f.created DESC""", {"s": status})}


def set_followup_status(g, fid: str, status: str) -> dict:
    if status not in {"approved", "dismissed", "draft"}:
        raise ValueError("status must be approved, dismissed or draft")
    found = rows(g, "MATCH (f:FollowUp {id: $id}) SET f.status = $s RETURN f.id AS id, f.status AS status",
                 {"id": fid, "s": status})
    if not found:
        raise ValueError(f"{fid} not found")
    return found[0]


def read_cypher(g, query: str) -> dict:
    """Run a read-only Cypher query (writes are refused by FalkorDB's read-only mode)."""
    if WRITE_WORDS.search(query):
        raise ValueError("only read queries are allowed")
    res = g.ro_query(query)
    cols = [h[1] if isinstance(h, (list, tuple)) else h for h in res.header]
    data = [dict(zip(cols, [str(v) if not isinstance(v, (int, float, str, bool, type(None), list)) else v
                            for v in r])) for r in res.result_set[:50]]
    return {"columns": cols, "rows": data, "truncated": len(res.result_set) > 50}


TOOLS = {
    "scan": (scan, "Run the loose-end detectors: stale citations (open decisions resting on retired ones), "
                   "broken promises (missed release targets) and orphans (open work whose authors went quiet).",
             {"kind": {"type": "string", "enum": ["all", "stale", "broken_promises", "orphans"]},
              "limit": {"type": "integer", "description": "max items per list, default 10"}}, []),
    "ripple": (ripple, "What-if: if a decision (KEP) changes, which open decisions are affected, through which path, "
                       "and who owns them. Also lists unconfirmed suggested links.",
               {"kep": {"type": "integer"}}, ["kep"]),
    "who_can_take_over": (who_can_take_over, "Rank active people who could take over a decision, with reasons.",
                          {"kep": {"type": "integer"}}, ["kep"]),
    "in_force": (in_force, "Follow replacements forward to find which decision is in force today.",
                 {"kep": {"type": "integer"}}, ["kep"]),
    "get_decision": (get_decision, "Details of one decision: status, team, authors, links, past questions, follow-ups.",
                     {"kep": {"type": "integer"}}, ["kep"]),
    "search_decisions": (search_decisions, "Full-text search over decision titles and summaries.",
                         {"query": {"type": "string"}}, ["query"]),
    "person": (person, "What a person authored, reviewed and approved, and when they were last active.",
               {"handle": {"type": "string"}}, ["handle"]),
    "draft_followup": (draft_followup, "Save a DRAFT follow-up asking a person to fix or confirm a loose end. "
                                       "Only use when the user asks for action. A human approves it later.",
                       {"kep": {"type": "integer"}, "assignee": {"type": "string"},
                        "reason": {"type": "string", "description": "one sentence with the evidence"}},
                       ["kep", "assignee", "reason"]),
    "list_followups": (list_followups, "List follow-up tasks saved in the graph.",
                       {"status": {"type": "string", "enum": ["all", "draft", "approved", "dismissed"]}}, []),
    "read_cypher": (read_cypher, "Run a read-only Cypher query against the graph for questions the other tools "
                                 "don't cover. Labels: Decision, Person, Team, FeatureGate, Release, FollowUp. "
                                 "Edges: AUTHORED, REVIEWED, APPROVED, OWNED_BY, INVOLVES, GATED_BY, TARGETS, "
                                 "DEPENDS_ON, CITES, REPLACES, LIKELY_RELATED.",
                    {"query": {"type": "string"}}, ["query"]),
}


def schemas() -> list[dict]:
    return [{"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props, "required": req}}}
        for name, (_, desc, props, req) in TOOLS.items()]


def call(g, name: str, args: dict):
    if name not in TOOLS:
        raise ValueError(f"unknown tool {name}")
    return TOOLS[name][0](g, **(args or {}))
