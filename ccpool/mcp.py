"""MCP server so Claude Code can watch and steer the pool.

Served by the router at ``POST /mcp`` (streamable HTTP transport, JSON
responses), so a client container needs nothing installed:

    claude mcp add --transport http ccpool http://router:8787/mcp \
        --header "Authorization: Bearer $CCPOOL_ADMIN_KEY"

``ccpool mcp --url …`` is a stdio bridge to the same endpoint, for clients that
prefer stdio servers.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from typing import TYPE_CHECKING, Any, Callable

from aiohttp import web

from . import __version__

if TYPE_CHECKING:
    from .router import Router

PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")

INSTRUCTIONS = (
    "ccpool routes this Claude Code session's API traffic across several Claude Team seats "
    "and, when every seat is out of quota, paid backends (API key, Bedrock, LiteLLM). "
    "Use pool_status to see seats, quota and where sessions are routed; usage_report for "
    "tokens and spend; the other tools change routing. Changing policy or draining a "
    "backend affects every user of the pool, so confirm with the user first."
)


def _ago(ts: float | None) -> str:
    if not ts:
        return "-"
    d = int(ts - time.time())
    if d <= 0:
        return "now"
    if d >= 86400:
        return f"{d // 86400}d{(d % 86400) // 3600}h"
    return f"{d // 3600}h{(d % 3600) // 60:02d}m"


def format_status(s: dict) -> str:
    lines = ["BACKEND        TIER   STATE     5H    RESET    7D    RESET   TODAY$   SERVED"]
    for h in s["holders"]:
        q = h["quota"]
        state = ("drained" if h["drained"] else "down" if not h["reachable"]
                 else "ok" if h["usable"] else "cooling")
        today = ((h.get("usage") or {}).get("today") or {}).get("total") or {}
        tier = "seat" if h["tier"] == "subscription" else "paid"
        u5 = f"{q['util_5h'] * 100:.0f}%" if tier == "seat" else "-"
        u7 = f"{q['util_7d'] * 100:.0f}%" if tier == "seat" else "-"
        lines.append(
            f"{h['name']:<14} {tier:<6} {state:<8} {u5:>4} {_ago(q.get('reset_5h')) if tier == 'seat' else '-':>8}"
            f" {u7:>5} {_ago(q.get('reset_7d')) if tier == 'seat' else '-':>8}"
            f" {today.get('cost_usd', 0):>8.2f} {h['served']:>8}"
        )
    p = s["policy"]
    budget = "none" if p["paid_daily_budget_usd"] is None else f"${p['paid_daily_budget_usd']:.2f}"
    lines += [
        "",
        f"policy: switch at {p['switch_threshold'] * 100:.0f}% | paid fallback "
        f"{'on' if p['paid_fallback'] else 'off'} | paid budget/day {budget} "
        f"(spent ${s['paid_spent_today_usd']:.2f})",
        f"sessions: {len(s['sessions'])}",
    ]
    for k, b in list(s["sessions"].items())[:20]:
        lines.append(f"  {k[:46]:<46} -> {b['holder']}{' (pinned)' if b['pinned'] else ''}"
                     f"  {b['requests']} req, idle {b['idle_s']}s")
    return "\n".join(lines)


def format_usage(s: dict, period: str) -> str:
    key = "today" if period == "today" else "all"
    lines = [f"Usage ({'today, UTC' if key == 'today' else 'since holder start'})",
             "BACKEND        TIER   REQS     INPUT    OUTPUT  CACHE_RD  CACHE_WR       USD"]
    grand = {"seat": 0.0, "paid": 0.0}
    for h in s["holders"]:
        t = (((h.get("usage") or {}).get(key) or {}).get("total")) or {}
        tier = "seat" if h["tier"] == "subscription" else "paid"
        cost = t.get("cost_usd", 0.0)
        grand[tier] += cost
        lines.append(
            f"{h['name']:<14} {tier:<6} {t.get('requests', 0):>4} {t.get('input', 0):>9} {t.get('output', 0):>9}"
            f" {t.get('cache_read', 0):>9} {t.get('cache_write_5m', 0) + t.get('cache_write_1h', 0):>9}"
            f" {cost:>9.2f}{' *' if t.get('unpriced') else ''}"
        )
    lines += [
        "",
        f"paid spend: ${grand['paid']:.2f}   seat usage at API list price: ${grand['seat']:.2f} "
        "(what the seats saved)",
        "Prices are first-party list prices unless overridden; * = model not in price table.",
    ]
    return "\n".join(lines)


class Tool:
    def __init__(self, name: str, description: str, schema: dict, fn: Callable[[dict], Any],
                 read_only: bool = True):
        self.name, self.description, self.schema, self.fn, self.read_only = (
            name, description, schema, fn, read_only)

    def spec(self) -> dict:
        return {
            "name": self.name, "description": self.description,
            "inputSchema": {"type": "object", "additionalProperties": False, **self.schema},
            "annotations": {"readOnlyHint": self.read_only, "destructiveHint": False},
        }


class McpServer:
    def __init__(self, router: "Router"):
        self.r = router
        self.tools = {t.name: t for t in self._tools()}

    def _tools(self) -> list[Tool]:
        r = self.r
        return [
            Tool("pool_status",
                 "Seats and paid backends: state, 5h/7d quota used and reset times, today's spend, "
                 "routing policy, and which backend each Claude Code session is on.",
                 {"properties": {}},
                 lambda a: (format_status(s := r.snapshot()), s)),
            Tool("usage_report",
                 "Token usage and cost per backend. Paid rows are real spend; seat rows are priced at "
                 "API list price to show what the seats are worth.",
                 {"properties": {"period": {"type": "string", "enum": ["today", "all"], "default": "today"}}},
                 lambda a: (format_usage(s := r.snapshot(), a.get("period", "today")),
                            {h["name"]: h.get("usage") for h in s["holders"]})),
            Tool("session_backend",
                 "Which backend a Claude Code session is routed to. session_id is the Claude Code "
                 "session UUID (shown by /status, or in the status line).",
                 {"properties": {"session_id": {"type": "string"}}, "required": ["session_id"]},
                 lambda a: _json(r.session_info(a["session_id"]) or {"error": "unknown session"})),
            Tool("set_backend_enabled",
                 "Take a backend out of rotation (enabled=false; its sessions move elsewhere on their "
                 "next request) or put it back.",
                 {"properties": {"name": {"type": "string"}, "enabled": {"type": "boolean"}},
                  "required": ["name", "enabled"]},
                 lambda a: _json(r.set_enabled(a["name"], a["enabled"])), read_only=False),
            Tool("set_policy",
                 "Change routing policy for the whole pool. switch_threshold: move a session off its "
                 "seat once 5h or 7d usage passes this fraction. paid_fallback: allow API/Bedrock/"
                 "LiteLLM when seats are exhausted. paid_daily_budget_usd: stop paid fallback after "
                 "this much spend per UTC day (null = no limit).",
                 {"properties": {
                     "switch_threshold": {"type": "number", "exclusiveMinimum": 0, "maximum": 1},
                     "paid_fallback": {"type": "boolean"},
                     "paid_daily_budget_usd": {"type": ["number", "null"], "minimum": 0}}},
                 self._set_policy, read_only=False),
            Tool("pin_session",
                 "Force a Claude Code session onto one backend (backend=null removes the pin). The pin "
                 "is dropped automatically if that backend runs out of quota.",
                 {"properties": {"session_id": {"type": "string"}, "backend": {"type": ["string", "null"]}},
                  "required": ["session_id", "backend"]},
                 lambda a: _json(r.pin_session(a["session_id"], a.get("backend"))), read_only=False),
        ]

    def _set_policy(self, args: dict) -> tuple[str, Any]:
        self.r.policy.update(args)
        self.r._event("policy_changed", via="mcp", policy=asdict(self.r.policy))
        return _json(asdict(self.r.policy))

    # JSON-RPC ----------------------------------------------------------------

    def dispatch(self, msg: dict) -> dict | None:
        mid = msg.get("id")
        method = msg.get("method", "")
        params = msg.get("params") or {}
        if mid is None:  # notification
            return None
        try:
            if method == "initialize":
                want = params.get("protocolVersion")
                result = {
                    "protocolVersion": want if want in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "ccpool", "version": __version__},
                    "instructions": INSTRUCTIONS,
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": [t.spec() for t in self.tools.values()]}
            elif method == "tools/call":
                tool = self.tools.get(params.get("name"))
                if tool is None:
                    return _err(mid, -32602, f"unknown tool {params.get('name')!r}")
                try:
                    out = tool.fn(params.get("arguments") or {})
                    text, structured = out if isinstance(out, tuple) else (out, None)
                    result = {"content": [{"type": "text", "text": text}], "isError": False}
                    if isinstance(structured, dict):
                        result["structuredContent"] = structured
                except (KeyError, ValueError, TypeError) as e:
                    result = {"content": [{"type": "text", "text": f"error: {e}"}], "isError": True}
            else:
                return _err(mid, -32601, f"method not found: {method}")
        except Exception as e:  # never kill the transport over one bad call
            return _err(mid, -32603, str(e))
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    async def handle_http(self, req: web.Request, authorized: bool) -> web.StreamResponse:
        if not authorized:
            return web.json_response({"error": "unauthorized"}, status=401,
                                     headers={"WWW-Authenticate": "Bearer"})
        if req.method == "DELETE":
            return web.Response(status=204)
        if req.method != "POST":
            return web.Response(status=405, headers={"Allow": "POST"})
        try:
            payload = await req.json()
        except ValueError:
            return web.json_response(_err(None, -32700, "parse error"), status=400)
        if isinstance(payload, list):
            out = [r for r in (self.dispatch(m) for m in payload) if r is not None]
            return web.json_response(out) if out else web.Response(status=202)
        resp = self.dispatch(payload)
        return web.json_response(resp) if resp is not None else web.Response(status=202)


def _json(d: Any) -> tuple[str, Any]:
    return json.dumps(d, indent=2), d if isinstance(d, dict) else None


def _err(mid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}
