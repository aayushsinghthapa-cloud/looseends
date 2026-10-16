"""LooseEnds web app: JSON API, the single-page UI, and the MCP server over HTTP.

    uvicorn looseends.web:app --port 7860
"""
import contextlib
import json
import os

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import agent
from . import detectors as D
from . import tools as T
from .db import graph, rows

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
STATIC = os.path.join(HERE, "static")

_graph = None


def g():
    global _graph
    if _graph is None:
        _graph = graph()
    return _graph


def ensure_loaded():
    """Build the graph from the bundled snapshot the first time the app starts."""
    try:
        if rows(g(), "MATCH (m:Meta {key: 'current_release'}) RETURN m.value AS v"):
            return
    except Exception:
        pass
    from .ingest_keps import build

    build(None, g())


def _mcp_app():
    try:
        from mcp.server.transport_security import TransportSecuritySettings

        from .mcp_server import mcp

        # Stateless JSON mode suits a public demo; host checks are off so it works behind any domain.
        return mcp, mcp.streamable_http_app(
            streamable_http_path="/mcp", stateless_http=True, json_response=True,
            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
    except Exception:  # the web app still works without MCP over HTTP
        return None, None


_mcp, _mcp_asgi = _mcp_app()


@contextlib.asynccontextmanager
async def lifespan(_app):
    ensure_loaded()
    if _mcp is not None:
        async with _mcp.session_manager.run():
            yield
    else:
        yield


app = FastAPI(title="LooseEnds", lifespan=lifespan)


def _guard(fn, *args):
    try:
        return fn(*args)
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.get("/api/summary")
def summary():
    cur = D.current_release(g())
    counts = {"stale": len(D.stale_citations(g())), "broken_promises": len(D.broken_promises(g())),
              "orphans": len(D.orphans(g()))}
    totals = rows(g(), """MATCH (d:Decision) WITH count(d) AS decisions
                          MATCH (p:Person) WITH decisions, count(p) AS people
                          MATCH (t:Team) RETURN decisions, people, count(t) AS teams""")[0]
    return {"current_release": f"v1.{cur}", **counts, **totals,
            "llm": bool(agent.api_key()), "model": agent.MODEL if agent.api_key() else None}


@app.get("/api/loose-ends/{kind}")
def loose_ends(kind: str):
    fns = {"stale": D.stale_citations, "broken_promises": D.broken_promises, "orphans": D.orphans}
    if kind not in fns:
        raise HTTPException(404, "kind must be stale, broken_promises or orphans")
    return fns[kind](g())


@app.get("/api/decision/{kep}")
def decision(kep: int):
    d = _guard(T.get_decision, g(), kep)
    d["in_force"] = D.current_version(g(), kep)
    d["experts"] = D.next_experts(g(), kep)
    return d


@app.get("/api/ripple/{kep}")
def ripple(kep: int):
    r = _guard(D.ripple, g(), kep)
    numbers = {kep} | {x["number"] for x in r["affected"]} | {x["number"] for x in r["shared_gate"]} \
        | {x["number"] for x in r["suggested"]}
    chain = D.current_version(g(), kep).get("chain", [])
    numbers |= set(chain)
    numbers |= {x["n"] for x in rows(g(), "MATCH (:Decision {number: $n})-[:REPLACES]->(x) RETURN x.number AS n",
                                    {"n": kep})}
    nodes = rows(g(), """
        MATCH (d:Decision) WHERE d.number IN $ns
        RETURN d.number AS number, d.title AS title, d.status AS status,
               d.active AS active, d.retired AS retired""", {"ns": sorted(numbers)})
    edges = rows(g(), """
        MATCH (a:Decision)-[e:DEPENDS_ON|CITES|REPLACES|LIKELY_RELATED]->(b:Decision)
        WHERE a.number IN $ns AND b.number IN $ns
        RETURN a.number AS source, b.number AS target, type(e) AS type""", {"ns": sorted(numbers)})
    gates = rows(g(), """
        MATCH (x:Decision {number: $n})-[:GATED_BY]->(f:FeatureGate)<-[:GATED_BY]-(s:Decision)
        WHERE s.number <> $n RETURN DISTINCT f.name AS gate, s.number AS other""", {"n": kep})
    hops = {x["number"]: x["hops"] for x in r["affected"]}
    return {**r, "graph": {"nodes": nodes, "edges": edges, "gates": gates, "hops": hops, "center": kep}}


@app.get("/api/search")
def search(q: str):
    return T.search_decisions(g(), q)


class Question(BaseModel):
    question: str
    history: list[dict] = []


@app.post("/api/ask")
def ask(body: Question):
    if not body.question.strip():
        raise HTTPException(400, "Type a question first.")
    return agent.ask(g(), body.question.strip(), body.history)


@app.get("/api/followups")
def followups():
    return T.list_followups(g())["followups"]


class Status(BaseModel):
    status: str


@app.post("/api/followups/{fid}")
def followup_status(fid: str, body: Status):
    return _guard(T.set_followup_status, g(), fid, body.status)


@app.get("/api/benchmark")
def benchmark():
    path = os.path.join(ROOT, "benchmark", "results.json")
    if not os.path.exists(path):
        raise HTTPException(404, "Run python -m looseends.benchmark first.")
    with open(path) as f:
        return json.load(f)


@app.get("/healthz")
def healthz():
    return {"ok": True}


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))


# Mounted last so every route above wins; the MCP sub-app serves /mcp.
if _mcp_asgi is not None:
    app.mount("/", _mcp_asgi)
