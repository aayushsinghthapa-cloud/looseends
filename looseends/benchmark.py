"""Benchmark: can LooseEnds find hidden dependencies between decisions?

Every KEP can declare related KEPs in its `see-also` metadata. We treat those
295 declared links as ground truth, remove them from a separate copy of the
graph, and ask each method to rank all other decisions for every KEP that has
at least one declared link. A good method puts the hidden links near the top.

This is the core ability behind the ripple detector: in a real company most
dependencies are never declared, so LooseEnds has to infer them.

Methods compared:
  text      TF-IDF similarity of title + summary (stands in for document search)
  people    shared contributors, Adamic-Adar weighted (Cypher)
  gates     shared feature gates and teams (Cypher)
  mentions  one KEP's proposal text links to the other, plus 2-hop mentions (Cypher)
  graph     all graph signals combined (fixed weights, chosen before testing)
  hybrid    graph + text; the text weight is picked by 2-fold cross-validation

Usage:
    python -m looseends.benchmark
"""
import json
import math
import os
import random
import sys
from collections import defaultdict

from .db import graph, rows
from .ingest_keps import load, read_snapshot

BENCH_GRAPH = "looseends_bench"
OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "benchmark")
KS = (1, 5, 10)

# Fixed before running the benchmark; not tuned on the test links.
GRAPH_WEIGHTS = {"mention": 3.0, "mention_2hop": 0.5, "people": 1.0, "gates": 2.0,
                 "same_team": 0.3, "shared_teams": 0.2}


def build_bench_graph(keps):
    """Same data as the product graph, minus every declared link between decisions."""
    g = graph(BENCH_GRAPH)
    load(keps, g)
    g.query("MATCH ()-[r:DEPENDS_ON|CITES|REPLACES]->() DELETE r")
    known = {k["number"] for k in keps}
    pairs = [{"a": k["number"], "b": b} for k in keps for b in k["mentions"] if b in known]
    for i in range(0, len(pairs), 2000):
        g.query("""UNWIND $rows AS r MATCH (a:Decision {number: r.a}), (b:Decision {number: r.b})
                   CREATE (a)-[:MENTIONS]->(b)""", {"rows": pairs[i:i + 2000]})
    g.query("MATCH (p:Person)-->(d:Decision) WITH p, count(DISTINCT d) AS deg SET p.deg = deg")
    return g


def graph_features(g) -> dict[str, dict[tuple[int, int], float]]:
    """Every graph signal for every pair of decisions, computed in FalkorDB."""
    queries = {
        "mention": """MATCH (a:Decision)-[:MENTIONS]-(b:Decision) WHERE a <> b
                      RETURN DISTINCT a.number AS a, b.number AS b, 1.0 AS v""",
        "mention_2hop": """MATCH (a:Decision)-[:MENTIONS]-(x:Decision)-[:MENTIONS]-(b:Decision)
                           WHERE a <> b AND x <> a AND x <> b
                           RETURN a.number AS a, b.number AS b, toFloat(count(DISTINCT x)) AS v""",
        "people": """MATCH (a:Decision)<--(p:Person)-->(b:Decision) WHERE a <> b
                     WITH DISTINCT a, b, p
                     RETURN a.number AS a, b.number AS b, sum(1.0 / log(p.deg + 1.0)) AS v""",
        "gates": """MATCH (a:Decision)-[:GATED_BY]->(f:FeatureGate)<-[:GATED_BY]-(b:Decision) WHERE a <> b
                    RETURN a.number AS a, b.number AS b, toFloat(count(DISTINCT f)) AS v""",
        "same_team": """MATCH (a:Decision)-[:OWNED_BY]->(t:Team)<-[:OWNED_BY]-(b:Decision) WHERE a <> b
                        RETURN a.number AS a, b.number AS b, 1.0 AS v""",
        "shared_teams": """MATCH (a:Decision)-[:OWNED_BY|INVOLVES]->(t:Team)<-[:OWNED_BY|INVOLVES]-(b:Decision)
                           WHERE a <> b
                           RETURN a.number AS a, b.number AS b, toFloat(count(DISTINCT t)) AS v""",
    }
    return {name: {(r["a"], r["b"]): r["v"] for r in rows(g, q)} for name, q in queries.items()}


def text_similarity(keps) -> dict[tuple[int, int], float]:
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity

    nums = [k["number"] for k in keps]
    docs = [f"{k['title']} {k['title']} {k['summary']}" for k in keps]
    m = TfidfVectorizer(stop_words="english", sublinear_tf=True, ngram_range=(1, 2), min_df=1).fit_transform(docs)
    sim = cosine_similarity(m)
    return {(a, b): float(sim[i, j]) for i, a in enumerate(nums) for j, b in enumerate(nums) if i != j and sim[i, j] > 0}


def combine(features, weights):
    out = defaultdict(float)
    for name, w in weights.items():
        for pair, v in features[name].items():
            out[pair] += w * v
    return out


def evaluate(score: dict, truth: dict[int, set[int]], candidates: list[int], sources: list[int]) -> dict:
    """Recall@k, hits@1 and MRR. Ties count at their average position, so no method gains from tie order."""
    recall = {k: 0.0 for k in KS}
    mrr = 0.0
    for s in sources:
        scores = sorted((score.get((s, c), 0.0) for c in candidates if c != s), reverse=True)
        targets = truth[s]
        ranks = []
        for t in targets:
            v = score.get((s, t), 0.0)
            higher = sum(1 for x in scores if x > v)
            ties = sum(1 for x in scores if x == v)
            ranks.append(higher + (ties + 1) / 2)
        for k in KS:
            recall[k] += sum(1 for r in ranks if r <= k) / len(targets)
        mrr += 1 / min(ranks)
    n = len(sources)
    return {**{f"recall@{k}": round(recall[k] / n, 3) for k in KS}, "mrr": round(mrr / n, 3)}


def run(verbose=True):
    keps = read_snapshot()
    known = {k["number"] for k in keps}
    truth = {k["number"]: {b for b in k["see_also"] if b in known} for k in keps}
    truth = {s: t for s, t in truth.items() if t}
    sources = sorted(truth)
    candidates = sorted(known)

    g = build_bench_graph(keps)
    feats = graph_features(g)
    text = text_similarity(keps)
    g.delete()

    graph_score = combine(feats, GRAPH_WEIGHTS)
    no_mentions = combine(feats, {k: v for k, v in GRAPH_WEIGHTS.items() if not k.startswith("mention")})

    def hybrid_with(lam):
        out = defaultdict(float, graph_score)
        for pair, v in text.items():
            out[pair] += lam * v
        return out

    # Pick the text weight by 2-fold cross-validation: tune on one half of the KEPs, test on the other.
    random.Random(7).shuffle(shuffled := list(sources))
    folds = [sorted(shuffled[::2]), sorted(shuffled[1::2])]
    grid = [1, 2, 4, 8, 16, 32, 64]
    chosen = []
    hybrid_parts = []
    for i in (0, 1):
        tune, test = folds[i], folds[1 - i]
        best = max(grid, key=lambda lam: evaluate(hybrid_with(lam), truth, candidates, tune)["mrr"])
        chosen.append(best)
        hybrid_parts.append((len(test), evaluate(hybrid_with(best), truth, candidates, test)))
    total = sum(n for n, _ in hybrid_parts)
    hybrid = {m: round(sum(n * r[m] for n, r in hybrid_parts) / total, 3) for m in hybrid_parts[0][1]}

    results = {
        "task": "Recover hidden see-also links between Kubernetes Enhancement Proposals",
        "sources": len(sources),
        "hidden_links": sum(len(t) for t in truth.values()),
        "candidates_per_source": len(candidates) - 1,
        "methods": {
            "text (TF-IDF document search)": evaluate(text, truth, candidates, sources),
            "graph: shared people": evaluate(feats["people"], truth, candidates, sources),
            "graph: shared gates + teams": evaluate(
                combine(feats, {"gates": 2.0, "same_team": 0.3, "shared_teams": 0.2}), truth, candidates, sources),
            "graph: proposal mentions": evaluate(
                combine(feats, {"mention": 3.0, "mention_2hop": 0.5}), truth, candidates, sources),
            "graph: all except mentions": evaluate(no_mentions, truth, candidates, sources),
            "graph: all signals": evaluate(graph_score, truth, candidates, sources),
            "hybrid: graph + text": hybrid,
        },
        "hybrid_text_weight_per_fold": chosen,
        "graph_weights": GRAPH_WEIGHTS,
    }
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "results.json"), "w") as f:
        json.dump(results, f, indent=2)
    with open(os.path.join(OUT_DIR, "RESULTS.md"), "w") as f:
        f.write(markdown(results))
    if verbose:
        print(markdown(results))
    return results


def markdown(r) -> str:
    lines = [
        "# Benchmark results",
        "",
        f"**Task:** {r['task']}. {r['hidden_links']} declared links across {r['sources']} KEPs were hidden; "
        f"each method ranks {r['candidates_per_source']} candidates per KEP.",
        "",
        "| Method | Hits@1 | Recall@5 | Recall@10 | MRR |",
        "|---|---|---|---|---|",
    ]
    for name, m in r["methods"].items():
        lines.append(f"| {name} | {m['recall@1']:.3f} | {m['recall@5']:.3f} | {m['recall@10']:.3f} | {m['mrr']:.3f} |")
    lines += [
        "",
        "Hits@1 is the share of hidden links ranked first. Recall@k is the share of hidden links found in the top k. "
        "MRR is the average of 1 / rank of the first correct answer. Higher is better for all.",
        "",
        f"Graph weights were fixed before testing: {r['graph_weights']}. "
        f"The hybrid's text weight was chosen by 2-fold cross-validation (per fold: {r['hybrid_text_weight_per_fold']}), "
        "so it is never tuned on the KEPs it is scored on.",
        "",
        "Reproduce with `python -m looseends.benchmark`.",
        "",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    run(verbose="-q" not in sys.argv)
