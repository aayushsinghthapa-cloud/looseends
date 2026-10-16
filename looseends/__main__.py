"""LooseEnds command line.

    python -m looseends load [path/to/enhancements]   build the graph (default: bundled snapshot)
    python -m looseends scan                          run every detector
    python -m looseends ripple 583                    what-if a decision changes
    python -m looseends experts 5832                  who could take over
    python -m looseends current 583                   which decision is in force now
    python -m looseends ... --json                    machine-readable output
"""
import argparse
import json
import sys

from . import detectors as D
from .db import graph


def owners_text(owners):
    return ", ".join(f"@{o['handle']}" for o in owners) or "no listed author"


def cmd_scan(g, as_json):
    report = {
        "current_release": D.current_release(g),
        "stale_citations": D.stale_citations(g),
        "broken_promises": D.broken_promises(g),
        "orphans": D.orphans(g),
    }
    if as_json:
        return report
    cur = report["current_release"]
    print(f"LooseEnds scan, current release v1.{cur}\n")
    print(f"STALE CITATIONS ({len(report['stale_citations'])}): open decisions resting on retired ones")
    for r in report["stale_citations"]:
        repl = f", now replaced by KEP-{r['replaced_by'][0]}" if r["replaced_by"] else ""
        print(f"  [{r['severity']}] KEP-{r['number']} {r['title']}")
        print(f"         relies on KEP-{r['old_number']} ({r['old_status']}{repl}); owners {owners_text(r['owners'])}")
    print(f"\nBROKEN PROMISES ({len(report['broken_promises'])}): release targets that passed unmet")
    for r in report["broken_promises"][:15]:
        print(f"  KEP-{r['number']} promised {r['promised']} by v1.{r['due']}, still {r['reached']} "
              f"({r['releases_late']} releases late) {r['title'][:60]}")
    if len(report["broken_promises"]) > 15:
        print(f"  ... and {len(report['broken_promises']) - 15} more (use --json)")
    print(f"\nORPHANS ({len(report['orphans'])}): open decisions whose authors have gone quiet")
    for r in report["orphans"][:15]:
        print(f"  KEP-{r['number']} {r['title'][:60]}, authors quiet {r['releases_quiet']} releases "
              f"({', '.join('@' + a for a in r['authors'])})")
    if len(report["orphans"]) > 15:
        print(f"  ... and {len(report['orphans']) - 15} more (use --json)")
    return None


def cmd_ripple(g, number, hops, as_json):
    result = D.ripple(g, number, hops)
    if as_json:
        return result
    d = result["decision"]
    open_hits = [r for r in result["affected"] if r["open"]]
    print(f"If KEP-{d['number']} '{d['title']}' changes, {len(open_hits)} open decisions are affected:\n")
    for r in result["affected"]:
        flag = "OPEN  " if r["open"] else "closed"
        path = " -> ".join(f"KEP-{n}" for n in r["path"])
        print(f"  {flag} {r['hops']} hop(s)  KEP-{r['number']} {r['title'][:55]}")
        print(f"         path: {path}; owners {owners_text(r['owners'])}")
    if result["shared_gate"]:
        print("\n  Shares a feature gate with:")
        for r in result["shared_gate"]:
            print(f"    KEP-{r['number']} {r['title'][:55]} (gates: {', '.join(r['shared_gates'])})")
    return None


def cmd_experts(g, number, as_json):
    result = D.next_experts(g, number)
    if as_json:
        return result
    print(f"Who could take over KEP-{number}:\n")
    for p in result:
        print(f"  @{p['handle']} (score {p['score']}, active v1.{p['last_active']})")
        for why in p["reasons"]:
            print(f"      - {why}")
    return None


def cmd_current(g, number, as_json):
    result = D.current_version(g, number)
    if as_json:
        return result
    chain = " -> replaced by -> ".join(f"KEP-{n}" for n in result["chain"])
    print(f"In force today: KEP-{result['in_force']} {result['title']}\n  chain: {chain}")
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(prog="looseends", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true", help="print JSON")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("load")
    p.add_argument("path", nargs="?", default=None, help="kubernetes/enhancements checkout (default: data/keps.json)")
    sub.add_parser("scan")
    p = sub.add_parser("ripple")
    p.add_argument("kep", type=int)
    p.add_argument("--hops", type=int, default=3)
    for name in ("experts", "current"):
        sub.add_parser(name).add_argument("kep", type=int)
    args = ap.parse_args(argv)

    if args.cmd == "load":
        from .ingest_keps import main as load_main
        sys.argv = ["ingest"] + ([args.path] if args.path else [])
        return load_main()
    g = graph()
    out = {"scan": lambda: cmd_scan(g, args.json),
           "ripple": lambda: cmd_ripple(g, args.kep, args.hops, args.json),
           "experts": lambda: cmd_experts(g, args.kep, args.json),
           "current": lambda: cmd_current(g, args.kep, args.json)}[args.cmd]()
    if out is not None:
        print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
