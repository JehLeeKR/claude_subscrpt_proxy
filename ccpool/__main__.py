"""Command line: ``python -m ccpool {holder,router,status}``."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.request

from aiohttp import web

from .credentials import CredentialError, load_credential
from .holder import DEFAULT_UPSTREAM, Holder
from .router import HolderRef, Router


def _holder(a: argparse.Namespace) -> None:
    try:
        cred = load_credential(token_file=a.token_file, oauth_file=a.oauth_file)
    except CredentialError as e:
        sys.exit(f"ccpool holder: {e}")
    h = Holder(a.name, cred, upstream=a.upstream, internal_key=os.environ.get("CCPOOL_INTERNAL_KEY"))
    web.run_app(h.app(), host=a.host, port=a.port, print=None)


def _router(a: argparse.Namespace) -> None:
    refs = []
    for spec in a.holder:
        name, sep, url = spec.partition("=")
        if not sep:
            sys.exit(f"--holder must be NAME=URL, got {spec!r}")
        refs.append(HolderRef(name=name, url=url.rstrip("/")))
    pool_key = os.environ.get("CCPOOL_POOL_KEY")
    if not pool_key:
        logging.warning("CCPOOL_POOL_KEY is not set: anyone who can reach the router can use the pool")
    r = Router(
        refs,
        pool_key=pool_key,
        internal_key=os.environ.get("CCPOOL_INTERNAL_KEY"),
        switch_threshold=a.switch_threshold,
        poll_interval=a.poll_interval,
    )
    web.run_app(r.app(), host=a.host, port=a.port, print=None)


def _fmt_reset(ts: float | None) -> str:
    if not ts:
        return "-"
    d = int(ts - time.time())
    if d <= 0:
        return "now"
    return f"{d // 3600}h{(d % 3600) // 60:02d}m"


def _status(a: argparse.Namespace) -> None:
    req = urllib.request.Request(a.url.rstrip("/") + "/_ccpool/status")
    key = os.environ.get("CCPOOL_POOL_KEY")
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.load(r)
    if a.json:
        print(json.dumps(data, indent=2))
        return
    print(f"{'HOLDER':<14}{'STATE':<10}{'5H':>6}{'RESET':>8}{'7D':>6}{'RESET':>9}{'SCORE':>7}{'SERVED':>8}")
    for h in data["holders"]:
        q = h["quota"]
        state = ("drained" if h["drained"] else "down" if not h["reachable"]
                 else "ok" if h["usable"] else "cooling")
        score = f"{q['score']:.2f}" if q.get("score") is not None else "-"
        print(f"{h['name']:<14}{state:<10}{q['util_5h']*100:>5.0f}%{_fmt_reset(q['reset_5h']):>8}"
              f"{q['util_7d']*100:>5.0f}%{_fmt_reset(q['reset_7d']):>9}{score:>7}{h['served']:>8}")
    print(f"\n{len(data['sessions'])} active session(s)")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="ccpool")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    h = sub.add_parser("holder", help="hold one seat's login and forward requests with it")
    h.add_argument("--name", required=True)
    g = h.add_mutually_exclusive_group()
    g.add_argument("--oauth-file", help="Claude Code .credentials.json (refreshed and rewritten in place)")
    g.add_argument("--token-file", help="file containing a token from `claude setup-token`")
    h.add_argument("--upstream", default=os.environ.get("CCPOOL_UPSTREAM", DEFAULT_UPSTREAM))
    h.add_argument("--host", default="0.0.0.0")
    h.add_argument("--port", type=int, default=8801)
    h.set_defaults(fn=_holder)

    r = sub.add_parser("router", help="the endpoint Claude Code points ANTHROPIC_BASE_URL at")
    r.add_argument("--holder", action="append", required=True, metavar="NAME=URL")
    r.add_argument("--switch-threshold", type=float, default=0.90,
                   help="move a session off its seat once 5h or 7d usage passes this (0-1)")
    r.add_argument("--poll-interval", type=float, default=15.0)
    r.add_argument("--host", default="0.0.0.0")
    r.add_argument("--port", type=int, default=8787)
    r.set_defaults(fn=_router)

    s = sub.add_parser("status", help="show the pool")
    s.add_argument("--url", default=os.environ.get("CCPOOL_URL", "http://localhost:8787"))
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=_status)

    a = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    a.fn(a)


if __name__ == "__main__":
    main()
