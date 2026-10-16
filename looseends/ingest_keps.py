"""Load Kubernetes Enhancement Proposals (KEPs) into the LooseEnds graph.

Data source: https://github.com/kubernetes/enhancements (public, Apache-2.0).
Each KEP is a decision with owners, reviewers, approvers, an owning team (SIG),
feature gates, release targets, and links to other KEPs.

Usage:
    python -m looseends.ingest_keps                       # from data/keps.json (bundled)
    python -m looseends.ingest_keps path/to/enhancements  # re-parse a fresh checkout
"""
import glob
import json
import os
import re
import sys

import yaml

from .db import graph

# Status values that mean a decision is no longer in force.
RETIRED_STATUSES = {"replaced", "withdrawn", "rejected", "superseded", "removed", "deferred"}
# Status values that mean work is still open (a promise not yet kept).
ACTIVE_STATUSES = {"provisional", "implementable"}

KEP_PATH_RE = re.compile(r"keps/(sig-[a-z0-9-]+|prod-readiness|provider-[a-z0-9-]+)/(\d+)-")
ISSUE_RE = re.compile(r"kubernetes/enhancements/issues/(\d+)")
HANDLE_RE = re.compile(r"@([A-Za-z0-9-]+)")


def milestone_num(value) -> int | None:
    """'v1.38' -> 38. Unknown or placeholder values -> None."""
    m = re.match(r"v?1\.(\d+)$", str(value or "").strip())
    return int(m.group(1)) if m else None


# Approximate Kubernetes release dates, used only when a KEP lists no milestone.
RELEASE_ANCHORS = [(2015, 7, 0), (2018, 3, 10), (2020, 12, 20), (2024, 4, 30), (2026, 8, 37)]


def release_from_date(value) -> int | None:
    m = re.match(r"(\d{4})-(\d{2})", str(value or ""))
    if not m:
        return None
    t = int(m.group(1)) * 12 + int(m.group(2))
    pts = [(y * 12 + mo, r) for y, mo, r in RELEASE_ANCHORS]
    for (t0, r0), (t1, r1) in zip(pts, pts[1:]):
        if t <= t1:
            return max(0, round(r0 + (t - t0) * (r1 - r0) / (t1 - t0)))
    return pts[-1][1] + round((t - pts[-1][0]) / 4)


def handle(raw) -> str | None:
    s = str(raw or "").strip()
    if not s or s.upper() == "TBD" or "@" not in s and " " in s:
        return None
    m = HANDLE_RE.search(s)
    return (m.group(1) if m else s).lower()


def clean_status(raw) -> str:
    s = str(raw or "").strip().lower()
    if "|" in s:
        return "unknown"
    if s.startswith("imlpemented") or s.startswith("implemented"):
        return "implemented"
    return s or "unknown"


def summary_of(readme: str) -> str:
    readme = re.sub(r"<!--.*?-->", "", readme, flags=re.S)
    m = (re.search(r"^##+ Summary\s*\n(.*?)(?=^##+ )", readme, re.S | re.M)
         or re.search(r"^##+ (?:Motivation|Overview|Abstract)\s*\n(.*?)(?=^##+ )", readme, re.S | re.M))
    text = m.group(1) if m else readme
    # Keep prose only: drop headings, list items (tables of contents), tables and HTML.
    lines = [ln for ln in text.splitlines()
             if ln.strip() and not re.match(r"\s*(#|[-*+] |\d+\. |\||<|```)", ln)]
    text = " ".join(lines)[:1500]
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)  # markdown links -> their text
    text = re.sub(r"\[\^[^\]]*\]", "", text)  # footnote markers
    text = text.replace("`", "").replace("**", "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:600]


def kep_numbers_in(values) -> set[int]:
    out = set()
    for v in values or []:
        m = KEP_PATH_RE.search(str(v))
        if m:
            out.add(int(m.group(2)))
    return out


def parse(repo: str):
    keps = []
    for path in sorted(glob.glob(os.path.join(repo, "keps", "**", "kep.yaml"), recursive=True)):
        if "NNNN-kep-template" in path:
            continue
        try:
            d = yaml.safe_load(open(path, encoding="utf-8"))
        except (yaml.YAMLError, ValueError):  # a few files have malformed YAML or dates
            continue
        if not isinstance(d, dict) or not d.get("kep-number"):
            continue
        try:
            number = int(str(d["kep-number"]).strip())
        except ValueError:
            continue
        folder = os.path.dirname(path)
        readme_path = os.path.join(folder, "README.md")
        readme = open(readme_path, encoding="utf-8", errors="ignore").read() if os.path.exists(readme_path) else ""
        rel = os.path.relpath(folder, repo).replace(os.sep, "/")
        mentioned = {int(n) for _, n in KEP_PATH_RE.findall(readme)} | {int(n) for n in ISSUE_RE.findall(readme)}
        targets = d.get("milestone") if isinstance(d.get("milestone"), dict) else {}
        keps.append(
            {
                "number": number,
                "title": str(d.get("title") or "").strip(),
                "team": str(d.get("owning-sig") or "unknown").strip(),
                "participating": [str(s).strip() for s in d.get("participating-sigs") or []],
                "status": clean_status(d.get("status")),
                "stage": str(d.get("stage") or "").strip().lower() or None,
                "created": str(d.get("creation-date") or ""),
                "updated": str(d.get("last-updated") or d.get("creation-date") or ""),
                "latest": milestone_num(d.get("latest-milestone")),
                "latest_est": release_from_date(d.get("last-updated") or d.get("creation-date")),
                "path": rel,
                "url": f"https://github.com/kubernetes/enhancements/tree/master/{rel}",
                "summary": summary_of(readme),
                "authors": [h for h in map(handle, d.get("authors") or []) if h],
                "reviewers": [h for h in map(handle, d.get("reviewers") or []) if h],
                "approvers": [h for h in map(handle, d.get("approvers") or []) if h],
                "gates": [g["name"] for g in d.get("feature-gates") or [] if isinstance(g, dict) and g.get("name")],
                "see_also": kep_numbers_in(d.get("see-also")) - {number},
                "replaces": kep_numbers_in(d.get("replaces")) - {number},
                "superseded_by": kep_numbers_in(d.get("superseded-by")) - {number},
                "mentions": mentioned - {number},
                "targets": {k: milestone_num(v) for k, v in targets.items() if milestone_num(v)},
            }
        )
    return keps


def load(keps, g, inferred: list | None = None):
    try:
        g.delete()
    except Exception:
        pass
    known = {k["number"] for k in keps}
    # Latest release any KEP has actually been worked in (targets can be future plans).
    current = max(k["latest"] for k in keps if k["latest"])

    for idx in ("CREATE INDEX FOR (d:Decision) ON (d.number)",
                "CREATE INDEX FOR (p:Person) ON (p.handle)",
                "CREATE INDEX FOR (t:Team) ON (t.name)",
                "CREATE INDEX FOR (f:FeatureGate) ON (f.name)",
                "CREATE INDEX FOR (r:Release) ON (r.minor)",
                "CALL db.idx.fulltext.createNodeIndex('Decision', 'title', 'summary')"):
        g.query(idx)

    g.query("UNWIND range($lo, $hi) AS m MERGE (:Release {minor: m, name: 'v1.' + toString(m)})",
            {"lo": 0, "hi": current})
    g.query("MERGE (m:Meta {key: 'current_release'}) SET m.value = $c", {"c": current})

    decisions = []
    for kep in keps:
        row = {k: v for k, v in kep.items() if k in
               ("number", "title", "team", "status", "stage", "created", "updated", "path", "url", "summary")}
        # Last release this decision saw work in. Milestone if listed, else estimated from its dates.
        row["latest"] = kep["latest"] or (min(kep["latest_est"], current) if kep["latest_est"] else None)
        row["latest_estimated"] = kep["latest"] is None
        decisions.append(row)
    g.query("""
        UNWIND $rows AS r
        CREATE (d:Decision {number: r.number, title: r.title, status: r.status, stage: r.stage,
                            created: r.created, updated: r.updated, latest: r.latest,
                            latest_estimated: r.latest_estimated,
                            path: r.path, url: r.url, summary: r.summary, source: 'kep'})
        MERGE (t:Team {name: r.team})
        CREATE (d)-[:OWNED_BY]->(t)
    """, {"rows": decisions})

    def edges(query, pairs):
        for i in range(0, len(pairs), 2000):
            g.query(query, {"rows": pairs[i:i + 2000]})

    people = []
    for k in keps:
        for role, key in (("AUTHORED", "authors"), ("REVIEWED", "reviewers"), ("APPROVED", "approvers")):
            people += [{"n": k["number"], "h": h, "role": role} for h in dict.fromkeys(k[key])]
    for role in ("AUTHORED", "REVIEWED", "APPROVED"):
        edges(f"""UNWIND $rows AS r MATCH (d:Decision {{number: r.n}})
                  MERGE (p:Person {{handle: r.h}}) CREATE (p)-[:{role}]->(d)""",
              [p for p in people if p["role"] == role])

    edges("""UNWIND $rows AS r MATCH (d:Decision {number: r.n})
             MERGE (t:Team {name: r.t}) CREATE (d)-[:INVOLVES]->(t)""",
          [{"n": k["number"], "t": t} for k in keps for t in dict.fromkeys(k["participating"]) if t != k["team"]])

    edges("""UNWIND $rows AS r MATCH (d:Decision {number: r.n})
             MERGE (f:FeatureGate {name: r.f}) CREATE (d)-[:GATED_BY]->(f)""",
          [{"n": k["number"], "f": f} for k in keps for f in dict.fromkeys(k["gates"])])

    edges("""UNWIND $rows AS r MATCH (d:Decision {number: r.n}), (rel:Release {minor: r.m})
             CREATE (d)-[:TARGETS {stage: r.s}]->(rel)""",
          [{"n": k["number"], "s": s, "m": m} for k in keps for s, m in k["targets"].items()])

    # Explicit, human-declared links from kep.yaml metadata.
    see_also = [{"a": k["number"], "b": b} for k in keps for b in k["see_also"] if b in known]
    edges("""UNWIND $rows AS r MATCH (a:Decision {number: r.a}), (b:Decision {number: r.b})
             CREATE (a)-[:DEPENDS_ON {source: 'see-also', confidence: 1.0}]->(b)""", see_also)

    replaces = {(k["number"], b) for k in keps for b in k["replaces"] if b in known}
    replaces |= {(b, k["number"]) for k in keps for b in k["superseded_by"] if b in known}
    edges("""UNWIND $rows AS r MATCH (new:Decision {number: r.a}), (old:Decision {number: r.b})
             CREATE (new)-[:REPLACES {source: 'kep.yaml'}]->(old)""",
          [{"a": a, "b": b} for a, b in sorted(replaces)])

    # Weaker links: another KEP mentioned in the proposal text.
    explicit = {(r["a"], r["b"]) for r in see_also} | replaces
    cites = [{"a": k["number"], "b": b} for k in keps for b in k["mentions"]
             if b in known and (k["number"], b) not in explicit and (b, k["number"]) not in replaces]
    edges("""UNWIND $rows AS r MATCH (a:Decision {number: r.a}), (b:Decision {number: r.b})
             CREATE (a)-[:CITES {source: 'readme', confidence: 0.6}]->(b)""", cites)

    # Suggested links nobody declared (see infer.py). Kept separate so they are never treated as fact.
    if inferred:
        edges("""UNWIND $rows AS r MATCH (a:Decision {number: r.a}), (b:Decision {number: r.b})
                 CREATE (a)-[:LIKELY_RELATED {source: 'inferred', score: r.score,
                                             confidence: r.confidence, reasons: r.reasons}]->(b)""",
              [r for r in inferred if r["a"] in known and r["b"] in known])

    # Derived state, computed once in the graph so every query can use it.
    g.query("MATCH (d:Decision) SET d.active = d.status IN $active",
            {"active": sorted(ACTIVE_STATUSES)})
    g.query("""MATCH (d:Decision)
               OPTIONAL MATCH (n:Decision)-[:REPLACES]->(d)
               WITH d, count(n) AS replacedBy
               SET d.retired = d.status IN $retired OR replacedBy > 0""",
            {"retired": sorted(RETIRED_STATUSES)})
    # A person's last activity = most recent release of any KEP they took part in.
    g.query("""MATCH (p:Person)-[]->(d:Decision)
               WITH p, max(d.latest) AS last
               SET p.last_active = last""")
    return current


SNAPSHOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "keps.json")


def save_snapshot(keps, path=SNAPSHOT):
    """Write parsed KEPs to JSON so the graph can be rebuilt without cloning the repo."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    out = [{k: sorted(v) if isinstance(v, set) else v for k, v in kep.items()} for kep in keps]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1, sort_keys=True)


def read_snapshot(path=SNAPSHOT):
    with open(path, encoding="utf-8") as f:
        keps = json.load(f)
    for kep in keps:
        for key in ("see_also", "replaces", "superseded_by", "mentions"):
            kep[key] = set(kep[key])
        kep["targets"] = {k: v for k, v in kep["targets"].items()}
    return keps


def build(source: str | None = None, g=None):
    """Load from a kubernetes/enhancements checkout (and refresh the snapshot) or from the snapshot."""
    if source and os.path.isdir(source):
        keps = parse(source)
        save_snapshot(keps)
    else:
        keps = read_snapshot(source or SNAPSHOT)
    inferred_path = os.path.join(os.path.dirname(SNAPSHOT), "inferred.json")
    inferred = json.load(open(inferred_path)) if os.path.exists(inferred_path) else None
    g = g or graph()
    return keps, load(keps, g, inferred), g


def main():
    keps, current, g = build(sys.argv[1] if len(sys.argv) > 1 else None)
    stats = g.query("""
        MATCH (n) WITH labels(n)[0] AS label, count(*) AS c RETURN label, c ORDER BY c DESC""").result_set
    rels = g.query("MATCH ()-[r]->() RETURN type(r), count(*) ORDER BY count(*) DESC").result_set
    print(f"Loaded {len(keps)} KEPs. Current release: v1.{current}")
    print("Nodes:", ", ".join(f"{l} {c}" for l, c in stats))
    print("Edges:", ", ".join(f"{t} {c}" for t, c in rels))


if __name__ == "__main__":
    main()
