"""Suggest links between decisions that nobody declared.

Uses the method that scored best in the benchmark (graph signals + text
similarity) and keeps, for each decision, up to two suggestions that:
  - are not already linked in any way (declared, replaced or mentioned), and
  - have at least one graph signal and a combined score of at least MIN_SCORE.

On the benchmark, 45% of such suggestions (33 of 74, where chance is about
0.06%) match a link the authors declared themselves. That is a lower bound, because related pairs that were
never declared count as misses. So suggestions are shown as "likely related,
please confirm", never as facts.

Writes data/inferred.json, which the loader turns into LIKELY_RELATED edges.

Usage:
    python -m looseends.infer
"""
import json
import os
from collections import defaultdict

from .benchmark import GRAPH_WEIGHTS, build_bench_graph, combine, graph_features, text_similarity
from .db import rows
from .ingest_keps import read_snapshot

HYBRID_TEXT_WEIGHT = 16  # median choice of the benchmark's cross-validation
MIN_SCORE = 4.0
PER_DECISION = 2
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "inferred.json")


def suggest(keps):
    known = {k["number"] for k in keps}
    linked = defaultdict(set)
    for k in keps:
        for b in k["see_also"] | k["mentions"] | k["replaces"] | k["superseded_by"]:
            if b in known:
                linked[k["number"]].add(b)
                linked[b].add(k["number"])

    g = build_bench_graph(keps)
    feats = graph_features(g)
    text = text_similarity(keps)
    structural = combine(feats, GRAPH_WEIGHTS)
    score = defaultdict(float, structural)
    for pair, v in text.items():
        score[pair] += HYBRID_TEXT_WEIGHT * v

    best: dict[tuple[int, int], float] = {}
    for a in sorted(known):
        cands = sorted(((score[(a, b)], b) for b in known
                        if b != a and b not in linked[a] and structural.get((a, b), 0) > 0), reverse=True)
        for s, b in cands[:PER_DECISION]:
            if s >= MIN_SCORE:
                key = (min(a, b), max(a, b))
                best[key] = max(best.get(key, 0), s)

    pairs = [{"a": a, "b": b} for a, b in best]
    people = {(r["a"], r["b"]): r["who"] for r in rows(g, """
        UNWIND $rows AS r MATCH (a:Decision {number: r.a})<--(p:Person)-->(b:Decision {number: r.b})
        RETURN r.a AS a, r.b AS b, collect(DISTINCT p.handle) AS who""", {"rows": pairs})}
    gates = {(r["a"], r["b"]): r["names"] for r in rows(g, """
        UNWIND $rows AS r MATCH (a:Decision {number: r.a})-[:GATED_BY]->(f)<-[:GATED_BY]-(b:Decision {number: r.b})
        RETURN r.a AS a, r.b AS b, collect(DISTINCT f.name) AS names""", {"rows": pairs})}
    via = {(r["a"], r["b"]): r["via"] for r in rows(g, """
        UNWIND $rows AS r MATCH (a:Decision {number: r.a})-[:MENTIONS]-(x:Decision)-[:MENTIONS]-(b:Decision {number: r.b})
        RETURN r.a AS a, r.b AS b, collect(DISTINCT x.number) AS via""", {"rows": pairs})}
    team = {(r["a"], r["b"]): r["t"] for r in rows(g, """
        UNWIND $rows AS r MATCH (a:Decision {number: r.a})-[:OWNED_BY]->(t)<-[:OWNED_BY]-(b:Decision {number: r.b})
        RETURN r.a AS a, r.b AS b, t.name AS t""", {"rows": pairs})}
    g.delete()

    top = max(best.values()) if best else 1
    out = []
    for (a, b), s in sorted(best.items(), key=lambda x: -x[1]):
        reasons = []
        if gates.get((a, b)):
            reasons.append("share feature gate " + ", ".join(gates[(a, b)][:2]))
        if via.get((a, b)):
            reasons.append("both linked from " + ", ".join(f"KEP-{n}" for n in via[(a, b)][:3]))
        if people.get((a, b)):
            who = people[(a, b)]
            reasons.append(f"{len(who)} shared contributor{'s' if len(who) > 1 else ''} ("
                           + ", ".join("@" + h for h in who[:3]) + ")")
        if team.get((a, b)):
            reasons.append(f"same team {team[(a, b)]}")
        sim = text.get((a, b), 0)
        if sim >= 0.1:
            reasons.append(f"similar wording ({sim:.2f})")
        out.append({"a": a, "b": b, "score": round(s, 2), "confidence": round(min(1.0, s / top), 2),
                    "reasons": reasons})
    return out


def main():
    keps = read_snapshot()
    links = suggest(keps)
    with open(OUT, "w") as f:
        json.dump(links, f, indent=1)
    print(f"Wrote {len(links)} suggested links to {os.path.relpath(OUT)}")
    for link in links[:5]:
        print(f"  KEP-{link['a']} ~ KEP-{link['b']} ({link['score']}): {'; '.join(link['reasons'])}")


if __name__ == "__main__":
    main()
