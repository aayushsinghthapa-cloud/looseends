"""Agent and tool tests on the same tiny known graph (no API key, offline planner)."""
import os

import pytest

os.environ.pop("GROQ_API_KEY", None)
os.environ.pop("LLM_API_KEY", None)

from looseends import agent, tools  # noqa: E402
from looseends.db import graph  # noqa: E402
from tests.test_detectors import FIXTURE  # noqa: E402


@pytest.fixture()
def g():
    graph_ = graph("looseends_test_agent")
    try:
        graph_.delete()
    except Exception:
        pass
    graph_.query(FIXTURE)
    yield graph_
    graph_.delete()


def test_ripple_question_uses_graph_and_is_remembered(g):
    res = agent.ask(g, "What if KEP-1 changes?")
    assert res["mode"] == "offline planner"
    assert [t["tool"] for t in res["trace"]] == ["ripple"]
    assert "KEP-3" in res["answer"] and "KEP-4" in res["answer"]
    remembered = g.query("MATCH (r:AgentRun)-[:ABOUT]->(d:Decision {number: 1}) RETURN count(r)").result_set
    assert remembered[0][0] == 1


def test_followup_goes_to_active_person_when_authors_are_quiet(g):
    res = agent.ask(g, "please draft a follow up for KEP-5")
    assert "FU-1" in res["answer"]
    f = tools.list_followups(g)["followups"][0]
    assert f["assignee"] == "carol" and f["status"] == "draft"
    assert "releases late" in f["reason"]
    assert tools.set_followup_status(g, "FU-1", "approved")["status"] == "approved"


def test_read_cypher_refuses_writes(g):
    with pytest.raises(ValueError):
        tools.read_cypher(g, "MATCH (n) DETACH DELETE n")
    assert tools.read_cypher(g, "MATCH (d:Decision) RETURN count(d) AS n")["rows"] == [{"n": 5}]


def test_llm_loop_with_simulated_groq_server(g, monkeypatch):
    """Runs the real HTTP code path against a local server that speaks the OpenAI chat format."""
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    seen = []

    class Fake(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append(body)
            if not any(m["role"] == "tool" for m in body["messages"]):
                msg = {"role": "assistant", "content": None, "tool_calls": [{
                    "id": "call_1", "type": "function",
                    "function": {"name": "ripple", "arguments": json.dumps({"kep": 1})}}]}
            else:
                tool_out = json.loads([m for m in body["messages"] if m["role"] == "tool"][0]["content"])
                hit = tool_out["affected_open"][0]["number"]
                msg = {"role": "assistant", "content": f"KEP-{hit} is affected."}
            data = json.dumps({"choices": [{"message": msg}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    monkeypatch.setattr(agent, "BASE_URL", f"http://127.0.0.1:{server.server_port}")
    res = agent.ask(g, "what breaks if KEP-1 changes?")
    server.shutdown()
    assert res["mode"].startswith("LLM")
    assert res["answer"] == "KEP-3 is affected."
    assert [t["tool"] for t in res["trace"]] == ["ripple"]
    assert seen[0]["tools"] and seen[0]["messages"][0]["role"] == "system"
