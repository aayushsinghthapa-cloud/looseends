"""The LooseEnds agent.

With GROQ_API_KEY set (or LLM_API_KEY for any OpenAI-compatible endpoint), a
language model plans which graph tools to call, reads their results and writes
the answer. Without a key, a rule-based planner calls the same tools, so the
product works offline and in tests.

Every run returns the tool calls it made (the trace), and is saved in the
graph as an AgentRun linked to the decisions it touched, so later questions
can see what was asked before.
"""
import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone

from . import tools as T
from .db import rows

BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.groq.com/openai/v1")
MODEL = os.environ.get("LOOSEENDS_MODEL", "openai/gpt-oss-120b")
MAX_ROUNDS = 6

SYSTEM = """You are LooseEnds, an agent that finds knowledge that has quietly died inside an organisation:
decisions that changed while work built on them didn't, promised deadlines that passed, and work whose
owners went quiet. The organisation here is the Kubernetes project: decisions are KEPs (Kubernetes
Enhancement Proposals), teams are SIGs, people are GitHub handles.

Rules:
- Always use the tools. Never state a fact about a KEP or person that a tool did not return.
- Cite every claim with KEP numbers (KEP-123) and @handles, and show the evidence path when there is one,
  e.g. KEP-5547 -> KEP-5832 -> KEP-583.
- "Suggested links" are unconfirmed inferences. Say so whenever you mention one.
- Only call draft_followup when the user asks you to act (notify, follow up, assign, fix). Drafts are
  reviewed by a person before anything is sent; say that.
- Be brief: a short answer, then a bullet list of findings. No preamble."""


def api_key():
    return os.environ.get("GROQ_API_KEY") or os.environ.get("LLM_API_KEY")


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run_tool(g, name, args, trace):
    try:
        result = T.call(g, name, args)
        trace.append({"tool": name, "args": args, "ok": True})
    except Exception as e:  # tool errors go back to the planner instead of crashing the run
        result = {"error": str(e)}
        trace.append({"tool": name, "args": args, "ok": False, "error": str(e)})
    return result


def _chat(messages):
    body = json.dumps({"model": MODEL, "messages": messages, "tools": T.schemas(),
                       "tool_choice": "auto", "temperature": 0.2}).encode()
    req = urllib.request.Request(f"{BASE_URL}/chat/completions", data=body, headers={
        "Authorization": f"Bearer {api_key()}", "Content-Type": "application/json",
        "User-Agent": "looseends/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)["choices"][0]["message"]


def _llm_answer(g, question, history, trace):
    messages = [{"role": "system", "content": SYSTEM}]
    messages += [{"role": m["role"], "content": m["content"]} for m in (history or [])[-6:]
                 if m.get("role") in ("user", "assistant") and m.get("content")]
    messages.append({"role": "user", "content": question})
    for _ in range(MAX_ROUNDS):
        msg = _chat(messages)
        calls = msg.get("tool_calls") or []
        if not calls:
            return msg.get("content") or ""
        messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
        for c in calls:
            try:
                args = json.loads(c["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            result = _run_tool(g, c["function"]["name"], args, trace)
            messages.append({"role": "tool", "tool_call_id": c["id"],
                             "content": json.dumps(result, default=str)[:12000]})
    return "I ran out of steps before finishing. Here is what the tools returned so far; try a narrower question."


# ---------- Rule-based planner (no API key) ----------

def _owners(items):
    return ", ".join("@" + o["handle"] for o in items) or "no listed author"


def _offline_answer(g, question, trace):
    q = question.lower()
    keps = [int(n) for n in re.findall(r"kep[-\s#]*(\d{1,5})\b", q)] or \
           [int(n) for n in re.findall(r"(?<![v.\d])\b(\d{2,5})\b", q)]
    handles = re.findall(r"@([a-z0-9-]+)", q)
    out = []

    def run(name, **args):
        return _run_tool(g, name, args, trace)

    if any(w in q for w in ("follow up", "follow-up", "notify", "assign", "ask them", "draft")) and keps:
        n = keps[0]
        exp = run("who_can_take_over", kep=n)
        d = run("get_decision", kep=n)
        if "error" in d:
            return f"I couldn't find KEP-{n}."
        # Prefer an author who is still active; if every author went quiet, the best active expert.
        active_authors = [r["h"] for r in rows(g, """
            MATCH (p:Person)-[:AUTHORED]->(:Decision {number: $n}), (m:Meta {key: 'current_release'})
            WHERE p.last_active >= m.value - 2
            RETURN p.handle AS h ORDER BY p.last_active DESC""", {"n": n})]
        experts = [c["handle"] for c in exp.get("candidates", [])]
        target = (handles or active_authors or experts or [None])[0]
        if not target:
            return f"I couldn't find anyone to assign KEP-{n} to."
        reason = " ".join(d.get("loose_ends") or []) or f"KEP-{n} ({d['title']}) is {d['status']}; please confirm it is current."
        f = run("draft_followup", kep=n, assignee=target, reason=reason)
        return (f"Drafted follow-up **{f.get('id')}** for @{target} about KEP-{n}. "
                "It is saved as a draft in the graph; a person must approve it before anything is sent.\n\n"
                f"> {f.get('message', '').replace(chr(10), ' ')}")

    if keps and any(w in q for w in ("who", "take over", "expert", "owner", "replace them", "knows")):
        n = keps[0]
        r = run("who_can_take_over", kep=n)
        if "error" in r:
            return r["error"]
        out.append(f"Best active people to take over KEP-{n}:")
        for c in r["candidates"]:
            out.append(f"- **@{c['handle']}** (active v1.{c['last_active']}): " + "; ".join(c["reasons"]))
        return "\n".join(out) if r["candidates"] else f"No active candidates found near KEP-{n}."

    if keps and any(w in q for w in ("in force", "current", "replaced", "latest", "superseded", "today")):
        n = keps[0]
        r = run("in_force", kep=n)
        if not r:
            return f"I couldn't find KEP-{n}."
        chain = " → ".join(f"KEP-{x}" for x in r["chain"])
        if r["in_force"] == n:
            return f"KEP-{n} has not been replaced; it is still the decision in force."
        return f"KEP-{n} is no longer in force. Today's decision is **KEP-{r['in_force']} {r['title']}**.\n\nChain: {chain}"

    if keps:
        n = keps[0]
        r = run("ripple", kep=n)
        if "error" in r:
            return r["error"]
        d = r["decision"]
        opened = r["affected_open"]
        out.append(f"If **KEP-{n} {d['title']}** ({d['status']}) changes, {len(opened)} open decision"
                   f"{'s are' if len(opened) != 1 else ' is'} affected:")
        for x in opened:
            path = " → ".join(f"KEP-{p}" for p in x["path"])
            out.append(f"- KEP-{x['number']} {x['title']}: path {path}; owners {_owners(x['owners'])}")
        for x in r["shares_feature_gate"]:
            out.append(f"- KEP-{x['number']} {x['title']}: shares feature gate {', '.join(x['shared_gates'])}")
        for x in r["suggested_links_unconfirmed"][:3]:
            out.append(f"- *Suggested, unconfirmed:* KEP-{x['number']} {x['title']} ({'; '.join(x['reasons'][:2])})")
        if len(out) == 1:
            out.append("- Nothing downstream depends on it.")
        return "\n".join(out)

    if handles:
        r = run("person", handle=handles[0])
        if "error" in r:
            return r["error"]
        out.append(f"**@{r['handle']}**, last active {r['last_active']}, teams: {', '.join(r['teams']) or 'none'}.")
        for x in r["authored"][:5]:
            out.append(f"- authored KEP-{x['number']} {x['title']} ({x['status']})")
        return "\n".join(out)

    kind = ("stale" if any(w in q for w in ("stale", "retired", "outdated", "out of date")) else
            "broken_promises" if any(w in q for w in ("promise", "late", "deadline", "missed", "overdue")) else
            "orphans" if any(w in q for w in ("orphan", "quiet", "abandon", "left", "inactive")) else
            "all" if any(w in q for w in ("scan", "loose", "everything", "overview", "summary", "what's wrong")) else None)
    if kind:
        r = run("scan", kind=kind, limit=5)
        labels = {"stale": "Stale citations", "broken_promises": "Broken promises", "orphans": "Orphans"}
        out.append(f"Current release {r['current_release']}.")
        for k, label in labels.items():
            if k not in r:
                continue
            out.append(f"\n**{label}: {r[k]['total']}**")
            for x in r[k]["items"]:
                if k == "stale":
                    out.append(f"- KEP-{x['number']} {x['title']} relies on KEP-{x['old_number']} ({x['old_status']})")
                elif k == "broken_promises":
                    out.append(f"- KEP-{x['number']} promised {x['promised']} by v1.{x['due']}, still {x['reached']}")
                else:
                    out.append(f"- KEP-{x['number']} {x['title']}, authors quiet {x['releases_quiet']} releases")
        return "\n".join(out)

    r = run("search_decisions", query=question)
    if "error" in r or not r["results"]:
        return ("I couldn't match that. Try: 'scan for loose ends', 'what if KEP-583 changes', "
                "'who can take over KEP-5832', or 'what replaced KEP-583'.")
    out.append("Decisions matching your question:")
    for x in r["results"][:6]:
        out.append(f"- KEP-{x['number']} {x['title']} ({x['status']})")
    return "\n".join(out)


def _remember(g, question, answer, trace, mode):
    keps = set()
    for t in trace:
        if isinstance(t.get("args"), dict) and t["args"].get("kep"):
            keps.add(int(t["args"]["kep"]))
    keps |= {int(n) for n in re.findall(r"KEP-(\d+)", answer)}
    rows(g, """
        CREATE (r:AgentRun {question: $q, answer: $a, mode: $m, at: $at, tools: $tools})
        WITH r UNWIND $keps AS n MATCH (d:Decision {number: n}) CREATE (r)-[:ABOUT]->(d)""",
         {"q": question, "a": answer[:4000], "m": mode, "at": _now(),
          "tools": [t["tool"] for t in trace], "keps": sorted(keps)[:30]})


def ask(g, question: str, history: list | None = None) -> dict:
    trace: list[dict] = []
    mode = "offline planner"
    if api_key():
        try:
            answer = _llm_answer(g, question, history, trace)
            mode = f"LLM ({MODEL})"
        except (urllib.error.URLError, KeyError, TimeoutError, json.JSONDecodeError) as e:
            trace.append({"tool": "llm", "ok": False, "error": f"LLM unavailable ({e}); used offline planner"})
            answer = _offline_answer(g, question, trace)
    else:
        answer = _offline_answer(g, question, trace)
    _remember(g, question, answer, trace, mode)
    return {"answer": answer, "trace": trace, "mode": mode}


if __name__ == "__main__":
    import sys

    from .db import graph

    res = ask(graph(), " ".join(sys.argv[1:]) or "scan for loose ends")
    print(res["answer"])
    print(f"\n[{res['mode']}] tools: " + ", ".join(t["tool"] for t in res["trace"]))
