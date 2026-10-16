"""Detector tests on a tiny hand-built graph, so results are known in advance."""
import os

import pytest

os.environ.setdefault("LOOSEENDS_GRAPH", "looseends_test")

from looseends import detectors as D  # noqa: E402
from looseends.db import graph  # noqa: E402

FIXTURE = """
CREATE (:Meta {key: 'current_release', value: 30}),
       (r10:Release {minor: 10}), (r29:Release {minor: 29}),
       (t:Team {name: 'sig-x'}), (f:FeatureGate {name: 'Gate'}),
       (old:Decision {number: 1, title: 'Old rule', status: 'replaced', active: false, retired: true, latest: 20}),
       (new:Decision {number: 2, title: 'New rule', status: 'implementable', active: true, retired: false, latest: 30}),
       (dep:Decision {number: 3, title: 'Uses old rule', status: 'implementable', active: true, retired: false,
                      stage: 'alpha', latest: 30}),
       (far:Decision {number: 4, title: 'Two hops away', status: 'provisional', active: true, retired: false, latest: 29}),
       (late:Decision {number: 5, title: 'Late feature', status: 'implementable', active: true, retired: false,
                       stage: 'alpha', latest: 12}),
       (alice:Person {handle: 'alice', last_active: 30}),
       (bob:Person {handle: 'bob', last_active: 12}),
       (carol:Person {handle: 'carol', last_active: 30}),
       (new)-[:REPLACES]->(old),
       (dep)-[:DEPENDS_ON {source: 'see-also'}]->(old),
       (far)-[:CITES {source: 'readme'}]->(dep),
       (late)-[:TARGETS {stage: 'stable'}]->(r10),
       (dep)-[:TARGETS {stage: 'alpha'}]->(r29),
       (alice)-[:AUTHORED]->(dep), (bob)-[:AUTHORED]->(late),
       (carol)-[:REVIEWED]->(late), (alice)-[:AUTHORED]->(new),
       (dep)-[:OWNED_BY]->(t), (late)-[:OWNED_BY]->(t), (late)-[:GATED_BY]->(f)
"""


@pytest.fixture(scope="module")
def g():
    graph_ = graph("looseends_test")
    try:
        graph_.delete()
    except Exception:
        pass
    graph_.query(FIXTURE)
    yield graph_
    graph_.delete()


def test_stale_citation_found_with_replacement(g):
    hits = D.stale_citations(g)
    assert [(h["number"], h["old_number"], h["replaced_by"]) for h in hits] == [(3, 1, [2])]
    assert hits[0]["owners"][0]["handle"] == "alice"


def test_ripple_follows_two_hops(g):
    result = D.ripple(g, 1)
    assert {(r["number"], r["hops"]) for r in result["affected"]} == {(3, 1), (4, 2)}
    assert next(r for r in result["affected"] if r["number"] == 4)["path"] == [4, 3, 1]


def test_broken_promise_is_late(g):
    hits = D.broken_promises(g)
    assert [(h["number"], h["promised"], h["releases_late"]) for h in hits] == [(5, "stable", 20)]


def test_orphan_and_successor(g):
    assert [o["number"] for o in D.orphans(g)] == [5]
    experts = D.next_experts(g, 5)
    assert experts[0]["handle"] == "carol"
    assert "bob" not in [e["handle"] for e in experts]


def test_current_version_follows_replacements(g):
    assert D.current_version(g, 1)["in_force"] == 2
