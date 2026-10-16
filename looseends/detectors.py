"""LooseEnds detectors. Each one is a Cypher query over the FalkorDB graph.

Every result carries its evidence (the graph path that triggered it), so a
person can check why something was flagged.
"""
from .db import rows

# A person counts as inactive after this many releases with no KEP involvement
# (Kubernetes ships about three releases a year, so 4 is roughly 16 months).
INACTIVE_AFTER = 4
# A person counts as available to take over if active within this many releases.
ACTIVE_WITHIN = 2

LINK = "DEPENDS_ON|CITES"


def current_release(g) -> int:
    return rows(g, "MATCH (m:Meta {key: 'current_release'}) RETURN m.value AS v")[0]["v"]


def _owners(g, numbers: list[int]) -> dict[int, list[dict]]:
    if not numbers:
        return {}
    found = rows(g, """
        MATCH (p:Person)-[:AUTHORED]->(d:Decision) WHERE d.number IN $ns
        RETURN d.number AS n, p.handle AS handle, p.last_active AS last_active""", {"ns": numbers})
    out: dict[int, list[dict]] = {}
    for r in found:
        out.setdefault(r["n"], []).append({"handle": r["handle"], "last_active": r["last_active"]})
    return out


def stale_citations(g) -> list[dict]:
    """Active decisions that still depend on or cite a decision that has been retired."""
    found = rows(g, f"""
        MATCH (d:Decision)-[link:{LINK}]->(old:Decision)
        WHERE d.active AND NOT d.retired AND old.retired
        OPTIONAL MATCH (new:Decision)-[:REPLACES]->(old)
        RETURN d.number AS number, d.title AS title, d.status AS status,
               type(link) AS link, link.source AS link_source,
               old.number AS old_number, old.title AS old_title, old.status AS old_status,
               collect(DISTINCT new.number) AS replaced_by
        ORDER BY link DESC, number""")
    owners = _owners(g, [r["number"] for r in found])
    for r in found:
        r["owners"] = owners.get(r["number"], [])
        r["severity"] = "high" if r["link"] == "DEPENDS_ON" else "medium"
        r["evidence"] = (f"(KEP-{r['number']})-[:{r['link']} via {r['link_source']}]->"
                         f"(KEP-{r['old_number']} status={r['old_status']})")
    return found


def ripple(g, number: int, max_hops: int = 3) -> dict:
    """What-if: if decision `number` changes, which open decisions are affected, and who owns them?"""
    target = rows(g, "MATCH (d:Decision {number: $n}) RETURN d.number AS number, d.title AS title, "
                     "d.status AS status", {"n": number})
    if not target:
        raise ValueError(f"KEP-{number} not found")
    hits = rows(g, f"""
        MATCH p = (hit:Decision)-[:{LINK}*1..{int(max_hops)}]->(x:Decision {{number: $n}})
        WHERE hit.number <> $n
        WITH hit, min(length(p)) AS hops
        RETURN hit.number AS number, hit.title AS title, hit.status AS status,
               hit.active AND NOT hit.retired AS open, hops
        ORDER BY hops, number""", {"n": number})
    paths = {r["number"]: r["path"] for r in rows(g, f"""
        MATCH p = (hit:Decision)-[:{LINK}*1..{int(max_hops)}]->(x:Decision {{number: $n}})
        WHERE hit.number <> $n
        WITH hit, p ORDER BY length(p)
        WITH hit, head(collect(p)) AS p
        RETURN hit.number AS number, [node IN nodes(p) | node.number] AS path""", {"n": number})}
    # Decisions switched on by the same feature gate move together even without an explicit link.
    siblings = rows(g, """
        MATCH (x:Decision {number: $n})-[:GATED_BY]->(f:FeatureGate)<-[:GATED_BY]-(s:Decision)
        WHERE s.number <> $n
        RETURN s.number AS number, s.title AS title, s.status AS status,
               s.active AND NOT s.retired AS open, collect(f.name) AS shared_gates""", {"n": number})
    rank = {r["n"]: r["score"] for r in rows(g, f"""
        CALL algo.pageRank('Decision', 'DEPENDS_ON') YIELD node, score
        RETURN node.number AS n, score""")}
    # Suggested (undeclared) links, one hop, kept apart from declared ones.
    declared = {r["number"] for r in hits} | {r["number"] for r in siblings}
    suggested = [r for r in rows(g, """
        MATCH (x:Decision {number: $n})-[l:LIKELY_RELATED]-(s:Decision)
        RETURN s.number AS number, s.title AS title, s.status AS status,
               s.active AND NOT s.retired AS open, l.confidence AS confidence, l.reasons AS reasons
        ORDER BY l.score DESC""", {"n": number}) if r["number"] not in declared]
    owners = _owners(g, [r["number"] for r in hits + siblings + suggested])
    for r in suggested:
        r["owners"] = owners.get(r["number"], [])
    for r in hits:
        r["path"] = paths.get(r["number"], [])
        r["importance"] = round(rank.get(r["number"], 0) * 1000, 3)
        r["owners"] = owners.get(r["number"], [])
    for r in siblings:
        r["owners"] = owners.get(r["number"], [])
    hits.sort(key=lambda r: (not r["open"], r["hops"], -r["importance"]))
    return {"decision": target[0], "affected": hits, "shared_gate": siblings, "suggested": suggested}


def broken_promises(g) -> list[dict]:
    """Decisions that promised to reach a stage by a release that has already shipped."""
    cur = current_release(g)
    found = rows(g, """
        MATCH (d:Decision)-[t:TARGETS]->(r:Release)
        WHERE NOT d.retired AND d.status <> 'implemented' AND r.minor < $cur
          AND t.stage IN ['alpha', 'beta', 'stable']
          AND NOT (t.stage = 'alpha' AND d.stage IN ['alpha', 'beta', 'stable'])
          AND NOT (t.stage = 'beta' AND d.stage IN ['beta', 'stable'])
          AND NOT (t.stage = 'stable' AND d.stage = 'stable')
        WITH d, t.stage AS promised, max(r.minor) AS due
        RETURN d.number AS number, d.title AS title, d.status AS status, d.stage AS reached,
               promised, due, $cur - due AS releases_late, d.latest AS last_worked
        ORDER BY releases_late DESC, number""", {"cur": cur})
    owners = _owners(g, [r["number"] for r in found])
    for r in found:
        r["owners"] = owners.get(r["number"], [])
        r["evidence"] = (f"(KEP-{r['number']})-[:TARGETS {{stage:'{r['promised']}'}}]->(v1.{r['due']}); "
                         f"current is v1.{cur}, KEP is at stage '{r['reached']}'")
    return found


def orphans(g, inactive_after: int = INACTIVE_AFTER) -> list[dict]:
    """Open decisions whose every author has gone quiet."""
    cur = current_release(g)
    found = rows(g, """
        MATCH (p:Person)-[:AUTHORED]->(d:Decision)
        WHERE d.active AND NOT d.retired
        WITH d, collect(p) AS authors, max(p.last_active) AS newest
        WHERE newest IS NOT NULL AND newest <= $cutoff
        RETURN d.number AS number, d.title AS title, d.status AS status,
               [a IN authors | a.handle] AS authors, newest AS authors_last_active
        ORDER BY newest, number""", {"cutoff": cur - inactive_after})
    for r in found:
        r["releases_quiet"] = cur - r["authors_last_active"]
    return found


def next_experts(g, number: int, limit: int = 3) -> list[dict]:
    """Who could take over a decision: active people close to it in the graph, with reasons."""
    cur = current_release(g)
    params = {"n": number, "since": cur - ACTIVE_WITHIN}
    keep = "WHERE p.last_active >= $since AND NOT (p)-[:AUTHORED]->(d)"
    # Each signal is a separate graph pattern with a weight; closer involvement weighs more.
    signals = [
        (3, f"""MATCH (p:Person)-[:REVIEWED|APPROVED]->(d:Decision {{number: $n}}) {keep}
                RETURN p.handle AS handle, p.last_active AS last_active,
                       'reviewed or approved this KEP' AS why"""),
        (2, f"""MATCH (d:Decision {{number: $n}})-[:{LINK}]-(near:Decision)<-[:AUTHORED|APPROVED]-(p:Person) {keep}
                RETURN p.handle AS handle, p.last_active AS last_active,
                       'worked on linked KEP-' + toString(near.number) AS why"""),
        (2, f"""MATCH (d:Decision {{number: $n}})-[:GATED_BY]->(f:FeatureGate)<-[:GATED_BY]-(near:Decision)
                      <-[:AUTHORED]-(p:Person)
                WHERE near <> d AND p.last_active >= $since AND NOT (p)-[:AUTHORED]->(d)
                RETURN p.handle AS handle, p.last_active AS last_active,
                       'authored KEP sharing gate ' + f.name AS why"""),
        (1, f"""MATCH (d:Decision {{number: $n}})-[:OWNED_BY]->(t:Team)<-[:OWNED_BY]-(near:Decision)
                      <-[:APPROVED]-(p:Person)
                WHERE near <> d AND p.last_active >= $since AND NOT (p)-[:AUTHORED]->(d)
                RETURN DISTINCT p.handle AS handle, p.last_active AS last_active,
                       'approves in ' + t.name AS why"""),
    ]
    people: dict[str, dict] = {}
    for weight, query in signals:
        for r in rows(g, query, params):
            p = people.setdefault(r["handle"], {"handle": r["handle"], "score": 0,
                                                "last_active": r["last_active"], "reasons": []})
            p["score"] += weight
            if r["why"] not in p["reasons"]:
                p["reasons"].append(r["why"])
    ranked = sorted(people.values(), key=lambda p: (-p["score"], -(p["last_active"] or 0), p["handle"]))
    for p in ranked:
        p["reasons"] = p["reasons"][:3]
    return ranked[:limit]


def flags_for(g, number: int) -> list[str]:
    """Every loose end that involves one decision, as plain sentences with evidence."""
    out = []
    for r in stale_citations(g):
        if r["number"] == number:
            repl = f", which KEP-{r['replaced_by'][0]} replaced" if r["replaced_by"] else f" ({r['old_status']})"
            out.append(f"KEP-{number} {r['link'].lower().replace('_', ' ')} KEP-{r['old_number']}{repl}.")
        elif r["old_number"] == number:
            out.append(f"Open KEP-{r['number']} still relies on KEP-{number}, which is retired.")
    for r in broken_promises(g):
        if r["number"] == number:
            out.append(f"KEP-{number} promised {r['promised']} by v1.{r['due']} and is still "
                       f"{r['reached'] or 'not started'} ({r['releases_late']} releases late).")
    for r in orphans(g):
        if r["number"] == number:
            out.append(f"Every author of KEP-{number} has been quiet for {r['releases_quiet']} releases.")
    return out


def current_version(g, number: int) -> dict:
    """Follow REPLACES forward to the decision that is in force today."""
    found = rows(g, """
        MATCH (start:Decision {number: $n})
        OPTIONAL MATCH p = (newest:Decision)-[:REPLACES*1..5]->(start)
        WITH start, p, newest ORDER BY length(p) DESC
        WITH start, head(collect(p)) AS p, head(collect(newest)) AS newest
        RETURN start.number AS asked, start.status AS asked_status,
               coalesce(newest.number, start.number) AS in_force,
               coalesce(newest.title, start.title) AS title,
               CASE WHEN p IS NULL THEN [start.number]
                    ELSE reverse([x IN nodes(p) | x.number]) END AS chain""", {"n": number})
    return found[0] if found else {}
