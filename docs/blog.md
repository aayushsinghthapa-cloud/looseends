# LooseEnds: finding the knowledge your company forgot it lost

*Built for Graph Hacks 2026 (WeMakeDevs × FalkorDB), Company Brain track.*

Most "company brain" projects answer one question: *where is the document about X?* I wanted to answer a harder one: *which of our documents are now wrong, and why?*

Company knowledge rarely disappears. It decays in three quiet ways:

1. **It goes stale.** A decision changes, and every plan, runbook and proposal that relied on it keeps saying the old thing.
2. **It gets orphaned.** The person who owned it moves on, and nobody else knows it's theirs now.
3. **Promises lapse.** "We'll ship this by March" passes, and nobody notices because nothing records the promise and the date together.

None of these show up in search. Each document looks fine on its own. The problem only appears in the **connections**: this proposal depends on that decision, which was replaced; this work's every author has gone quiet. That's a graph problem, so I built LooseEnds on FalkorDB.

## The data: a real organisation's decisions

I needed real company knowledge, which is hard to get without breaking privacy. The Kubernetes project turned out to be a perfect stand-in. Every significant change goes through a **Kubernetes Enhancement Proposal (KEP)**: a written decision with authors, reviewers, approvers, an owning team (SIG), a status, release targets, feature gates and links to other KEPs. It's public, structured, and has years of history.

That's 679 decisions, 722 people and 38 teams: a mid-sized company's worth of institutional memory.

## The graph model

```
(:Person)-[:AUTHORED | REVIEWED | APPROVED]->(:Decision)
(:Decision)-[:OWNED_BY | INVOLVES]->(:Team)
(:Decision)-[:GATED_BY]->(:FeatureGate)
(:Decision)-[:TARGETS {stage}]->(:Release)
(:Decision)-[:DEPENDS_ON]->(:Decision)        declared in metadata
(:Decision)-[:CITES]->(:Decision)             mentioned in the proposal text
(:Decision)-[:REPLACES]->(:Decision)
(:Decision)-[:LIKELY_RELATED]->(:Decision)    inferred, never treated as fact
```

Two derived properties make the detectors simple. `Decision.retired` is true when a decision was withdrawn or rejected, or when anything `REPLACES` it. `Person.last_active` is the latest release of any decision the person touched.

## Three detectors, three Cypher queries

**Stale.** Open decisions resting on retired ones:

```cypher
MATCH (d:Decision)-[link:DEPENDS_ON|CITES]->(old:Decision)
WHERE d.active AND NOT d.retired AND old.retired
OPTIONAL MATCH (new:Decision)-[:REPLACES]->(old)
RETURN d.number, old.number, collect(new.number) AS replaced_by
```

On the real data this found 11, including a chain I didn't expect. *Coscheduling* (KEP-583) was replaced by *Decouple PodGroup API* (KEP-5832), which was itself replaced by *Gang Scheduling* (KEP-4671). Three open proposals still list KEP-5832 as a dependency.

**Missed targets.** A release target that shipped without the decision reaching that stage. There are 62 of them; one storage feature promised beta by v1.21 and is still alpha 17 releases later.

**Orphans.** Open decisions whose every author has had no involvement for four or more releases. There are 81.

Every finding carries the path that produced it, like `KEP-5547 → KEP-5832 → KEP-583`, so nobody has to trust a black box.

## The ripple: what breaks if this changes?

The most useful query turned out to be the what-if. Pick a decision, walk every dependency thread up to three hops, and rank what you hit:

```cypher
MATCH p = (hit:Decision)-[:DEPENDS_ON|CITES*1..3]->(x:Decision {number: $n})
WITH hit, min(length(p)) AS hops
RETURN hit.number, hops
```

Results are ordered with FalkorDB's built-in `algo.pageRank`, so the decisions many others rely on come first. FalkorDB was fast enough that I run this on every click in the UI.

## Who picks it up?

Flagging a problem is only half the job; someone has to own it. `who_can_take_over` scores active people with weighted graph signals:
- reviewed or approved the decision: 3
- worked on a linked decision: 2
- authored a decision that shares a feature gate: 2
- approves in the same team: 1

People who have gone quiet are excluded. For the Decouple PodGroup API it suggests a reviewer who also worked on two of the proposals that depend on it, which is exactly who I'd ask.

## The honest part: does the graph actually help?

It's easy to claim a graph beats search. I wanted a number.

Every KEP can declare related KEPs in its metadata, and there are 295 such links. I built a copy of the graph with all of them removed, then asked each method to find them again. For every KEP with a declared link, a method ranks all 677 other KEPs.

| Method | Found in top 10 |
|---|---|
| Text similarity alone (TF-IDF on title + summary) | 70% |
| Graph signals alone (shared people, gates, teams, mentions) | 76% |
| Graph + text | 89% |

I fixed the graph weights before running anything. The one tuned number, how much to weigh text, was chosen by 2-fold cross-validation, so it is never tuned on the KEPs it's scored on. The whole benchmark reruns with `python -m looseends.benchmark`.

The winning method then feeds back into the product. For links nobody declared, LooseEnds keeps up to two strong suggestions per decision and draws them as dashed "suggested, unconfirmed" threads. On the benchmark, 45% of suggestions with no mention in the text match a link the authors declared themselves (chance would be about 0.06%). That's a lower bound, so I show them as suggestions and never as facts.

## The agent, and agents using the agent

The chat agent (Groq's `openai/gpt-oss-120b`) can only answer through graph tools: `ripple`, `who_can_take_over`, `in_force`, `search_decisions`, a read-only Cypher tool, and `draft_followup`. Every answer shows the calls it made. Without an API key, a rule-based planner drives the same tools, so the demo never breaks.

It can also act. `draft_followup` writes a `FollowUp` node into the graph, addressed to an author who is still active (or the best expert if they've all gone quiet), with the evidence as the reason. Nothing is sent until a person approves it. Every question is saved too, as an `AgentRun` linked to the decisions it touched, so the next session can see what was already asked about a KEP.

The same tools are exposed over **MCP**, over stdio or HTTP at `/mcp`, so a coding agent can call `impact_of` before it changes something.

## What I learned

- **Model the "why", not just the "what".** `REPLACES` and `DEPENDS_ON` edges did more work than any text field.
- **Keep inferred knowledge separate.** A separate `LIKELY_RELATED` edge type with a confidence and reasons kept me honest in the UI and in the agent's prompt.
- **Measure before you claim.** The benchmark changed my design: text similarity was stronger than I expected, and the best results came from combining it with the graph rather than replacing it.

The code, graph model, queries and benchmark are on GitHub: **github.com/aayushsinghthapa-cloud/looseends**.
