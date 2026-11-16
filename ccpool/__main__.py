"""Command line: ``ccpool {holder,router,status,usage,drain,enable,statusline,mcp}``."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tomllib
import urllib.error
import urllib.request

from aiohttp import web

from .backends import ANTHROPIC_API, BedrockBackend, HttpBackend
from .credentials import CredentialError, load_credential
from .holder import Holder
from .mcp import format_status, format_usage
from .router import HolderRef, Policy, Router
from .usage import DEFAULT_PRICES, Ledger


def _read_secret(env: str | None, file: str | None, what: str) -> str:
    if file:
        return open(file).read().strip()
    v = os.environ.get(env or "")
    if not v:
        sys.exit(f"ccpool holder: {what} not found (set ${env} or pass a --*-file)")
    return v


def _load_json(path: str | None) -> dict:
    return json.load(open(path)) if path else {}


# holder --------------------------------------------------------------------

def _holder(a: argparse.Namespace) -> None:
    model_map = _load_json(a.model_map)
    if a.kind == "subscription":
        try:
            cred = load_credential(token_file=a.token_file, oauth_file=a.oauth_file)
        except CredentialError as e:
            sys.exit(f"ccpool holder: {e}")
        backend = HttpBackend("subscription", upstream=a.upstream or ANTHROPIC_API, credential=cred)
    elif a.kind == "api_key":
        backend = HttpBackend("api_key", upstream=a.upstream or ANTHROPIC_API, model_map=model_map,
                              api_key=_read_secret(a.key_env or "ANTHROPIC_API_KEY", a.key_file, "API key"))
    elif a.kind == "litellm":
        if not a.upstream:
            sys.exit("ccpool holder: --upstream (your LiteLLM proxy URL) is required for kind=litellm")
        backend = HttpBackend("litellm", upstream=a.upstream, model_map=model_map,
                              api_key=_read_secret(a.key_env or "LITELLM_API_KEY", a.key_file, "LiteLLM key"))
    else:
        region = a.region or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
        if not region:
            sys.exit("ccpool holder: --region (or AWS_REGION) is required for kind=bedrock")
        backend = BedrockBackend(region=region, profile=a.aws_profile, model_map=model_map,
                                 model_prefix=a.model_prefix, base_url=a.upstream)
    prices = {**DEFAULT_PRICES, **{k: tuple(v) for k, v in _load_json(a.prices).items()}}
    h = Holder(a.name, backend, internal_key=os.environ.get("CCPOOL_INTERNAL_KEY"), ledger=Ledger(prices=prices))
    web.run_app(h.app(), host=a.host, port=a.port, print=None)


# router --------------------------------------------------------------------

def _router(a: argparse.Namespace) -> None:
    refs: list[HolderRef] = []
    policy = Policy()
    if a.config:
        with open(a.config, "rb") as f:
            cfg = tomllib.load(f)
        policy.update(cfg.get("policy", {}))
        for b in cfg.get("backend", []):
            refs.append(HolderRef(name=b["name"], url=b["url"].rstrip("/"),
                                  tier=b.get("tier", "subscription"), priority=int(b.get("priority", 0))))
    for tier, specs in (("subscription", a.holder or []), ("paid", a.paid or [])):
        for i, spec in enumerate(specs):
            name, sep, url = spec.partition("=")
            if not sep:
                sys.exit(f"expected NAME=URL, got {spec!r}")
            refs.append(HolderRef(name=name, url=url.rstrip("/"), tier=tier, priority=i))
    if not refs:
        sys.exit("ccpool router: no backends (use --config, --holder or --paid)")
    if a.switch_threshold is not None:
        policy.switch_threshold = a.switch_threshold
    if a.no_paid_fallback:
        policy.paid_fallback = False
    if a.paid_budget is not None:
        policy.paid_daily_budget_usd = a.paid_budget

    pool_key = os.environ.get("CCPOOL_POOL_KEY")
    if not pool_key:
        logging.warning("CCPOOL_POOL_KEY is not set: anyone who can reach the router can use the pool")
    r = Router(refs, pool_key=pool_key, admin_key=os.environ.get("CCPOOL_ADMIN_KEY"),
               internal_key=os.environ.get("CCPOOL_INTERNAL_KEY"), policy=policy,
               poll_interval=a.poll_interval)
    web.run_app(r.app(), host=a.host, port=a.port, print=None)


# clients -------------------------------------------------------------------

def _call(url: str, path: str, method: str = "GET", body: dict | None = None, timeout: float = 10) -> dict:
    req = urllib.request.Request(url.rstrip("/") + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"content-type": "application/json"})
    key = os.environ.get("CCPOOL_ADMIN_KEY") or os.environ.get("CCPOOL_POOL_KEY") \
        or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _status(a):
    s = _call(a.url, "/_ccpool/status")
    print(json.dumps(s, indent=2) if a.json else format_status(s))


def _usage(a):
    s = _call(a.url, "/_ccpool/status")
    print(format_usage(s, a.period))


def _drain(a):
    print(_call(a.url, f"/_ccpool/holders/{a.name}/{a.cmd}", method="POST"))


def _statusline(a):
    """For Claude Code's statusLine: prints which backend this session is on."""
    try:
        sid = json.load(sys.stdin).get("session_id", "")
    except ValueError:
        sid = ""
    try:
        info = _call(a.url, f"/_ccpool/sessions/{sid}", timeout=2) if sid else None
    except urllib.error.HTTPError:
        info = None
    except OSError:
        print("ccpool: unreachable")
        return
    if not info:
        print("ccpool: (no requests yet)")
        return
    q = info.get("quota") or {}
    if info.get("tier") == "subscription":
        print(f"ccpool: {info['backend']} 5h {q.get('util_5h', 0) * 100:.0f}% · 7d {q.get('util_7d', 0) * 100:.0f}%"
              f"{' (pinned)' if info.get('pinned') else ''}")
    else:
        print(f"ccpool: {info['backend']} (paid fallback)")


def _mcp_stdio(a):
    """stdio <-> streamable-HTTP bridge to the router's /mcp endpoint."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req = urllib.request.Request(a.url.rstrip("/") + "/mcp", data=line.encode(), method="POST",
                                     headers={"content-type": "application/json",
                                              "accept": "application/json, text/event-stream"})
        key = os.environ.get("CCPOOL_ADMIN_KEY") or os.environ.get("CCPOOL_POOL_KEY")
        if key:
            req.add_header("Authorization", f"Bearer {key}")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                out = r.read()
        except urllib.error.HTTPError as e:
            msg = json.loads(line)
            if msg.get("id") is None:
                continue
            out = json.dumps({"jsonrpc": "2.0", "id": msg["id"],
                              "error": {"code": -32000, "message": f"router: HTTP {e.code}"}}).encode()
        except OSError as e:
            msg = json.loads(line)
            if msg.get("id") is None:
                continue
            out = json.dumps({"jsonrpc": "2.0", "id": msg["id"],
                              "error": {"code": -32000, "message": f"router unreachable: {e}"}}).encode()
        if out.strip():
            sys.stdout.write(out.decode().strip() + "\n")
            sys.stdout.flush()


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="ccpool")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    default_url = os.environ.get("CCPOOL_URL", "http://localhost:8787")

    h = sub.add_parser("holder", help="hold one backend's credential and forward requests with it")
    h.add_argument("--name", required=True)
    h.add_argument("--kind", choices=["subscription", "api_key", "litellm", "bedrock"], default="subscription")
    g = h.add_mutually_exclusive_group()
    g.add_argument("--oauth-file", help="subscription: Claude Code .credentials.json (kept refreshed)")
    g.add_argument("--token-file", help="subscription: token from `claude setup-token`")
    h.add_argument("--key-env", help="api_key/litellm: env var holding the key "
                                     "(default ANTHROPIC_API_KEY / LITELLM_API_KEY)")
    h.add_argument("--key-file", help="api_key/litellm: file holding the key")
    h.add_argument("--upstream", default=os.environ.get("CCPOOL_UPSTREAM"),
                   help="base URL (default api.anthropic.com; required for litellm)")
    h.add_argument("--region", help="bedrock: AWS region")
    h.add_argument("--aws-profile", help="bedrock: AWS profile (default: standard AWS credential chain)")
    h.add_argument("--model-prefix", default="anthropic.", help="bedrock: prefix added to model ids")
    h.add_argument("--model-map", help="JSON file mapping requested model -> backend model id")
    h.add_argument("--prices", help="JSON file overriding $/MTok prices: {model_prefix: [in, out, cache_read]}")
    h.add_argument("--host", default="0.0.0.0")
    h.add_argument("--port", type=int, default=8801)
    h.set_defaults(fn=_holder)

    r = sub.add_parser("router", help="the endpoint Claude Code points ANTHROPIC_BASE_URL at")
    r.add_argument("--config", help="router TOML file ([policy] and [[backend]] tables)")
    r.add_argument("--holder", action="append", metavar="NAME=URL", help="a subscription seat holder")
    r.add_argument("--paid", action="append", metavar="NAME=URL", help="a paid fallback holder, in priority order")
    r.add_argument("--switch-threshold", type=float)
    r.add_argument("--paid-budget", type=float, help="USD per UTC day across paid backends")
    r.add_argument("--no-paid-fallback", action="store_true")
    r.add_argument("--poll-interval", type=float, default=10.0)
    r.add_argument("--host", default="0.0.0.0")
    r.add_argument("--port", type=int, default=8787)
    r.set_defaults(fn=_router)

    for name, fn, helptext in (("status", _status, "show the pool"), ("usage", _usage, "token usage and spend")):
        s = sub.add_parser(name, help=helptext)
        s.add_argument("--url", default=default_url)
        if name == "status":
            s.add_argument("--json", action="store_true")
        else:
            s.add_argument("--period", choices=["today", "all"], default="today")
        s.set_defaults(fn=fn)

    for name in ("drain", "enable"):
        s = sub.add_parser(name, help=f"{name} a backend")
        s.add_argument("name")
        s.add_argument("--url", default=default_url)
        s.set_defaults(fn=_drain)

    s = sub.add_parser("statusline", help="one-line status for Claude Code's statusLine setting")
    s.add_argument("--url", default=default_url)
    s.set_defaults(fn=_statusline)

    s = sub.add_parser("mcp", help="stdio MCP server (bridges to the router's /mcp)")
    s.add_argument("--url", default=default_url)
    s.set_defaults(fn=_mcp_stdio)

    a = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    a.fn(a)


if __name__ == "__main__":
    main()
