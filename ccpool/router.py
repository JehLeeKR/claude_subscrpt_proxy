"""Router: the single endpoint Claude Code talks to.

Clients set ``ANTHROPIC_BASE_URL`` to the router and ``ANTHROPIC_AUTH_TOKEN``
to the pool key. For each request the router:

1. uses a **subscription** seat while any has quota, ranked by headroom and
   soon-expiring quota, keeping each Claude Code session on one seat while it
   stays under the switch threshold;
2. falls back to **paid** backends (API key, Bedrock, LiteLLM) in priority order
   only when every seat is exhausted, and only while the daily budget allows;
3. moves a session back to a seat as soon as one has quota again;
4. fails over within the same request on 429/401/5xx/unreachable.

Endpoints
  /*                                  forwarded to the chosen holder
  POST /mcp                           MCP (streamable HTTP) for monitoring and control
  GET  /_ccpool/status                everything (holders, quota, usage, sessions, policy)
  GET  /_ccpool/sessions/{id}         which backend a Claude Code session is on
  GET|POST /_ccpool/policy            read / change routing policy
  POST /_ccpool/holders/{name}/{drain|enable}
  POST|DELETE /_ccpool/sessions/{id}/pin
  GET  /_ccpool/healthz
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field

import aiohttp
from aiohttp import web

from .forward import key_ok, new_client_session, presented_key, request_headers, stream_back
from .quota import Quota

log = logging.getLogger(__name__)

FAILOVER_STATUSES = {401, 403, 429, 500, 502, 503, 529}
SESSION_HEADERS = ("x-claude-code-session-id", "x-session-id")
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
SESSION_RE = re.compile(r"session(?:_id)?[\"':=_ ]*(" + _UUID + ")", re.I)


@dataclass
class HolderRef:
    name: str
    url: str
    tier: str = "subscription"  # or "paid"
    priority: int = 0  # paid backends: lower is tried first
    quota: Quota = field(default_factory=Quota)
    usage: dict = field(default_factory=dict)
    backend: dict = field(default_factory=dict)
    reachable: bool = True
    drained: bool = False
    inflight: int = 0
    served: int = 0

    def usable(self, now: float) -> bool:
        return self.reachable and not self.drained and self.quota.available(now)

    def cost_today(self) -> float:
        return float(((self.usage.get("today") or {}).get("total") or {}).get("cost_usd") or 0.0)


@dataclass
class Policy:
    switch_threshold: float = 0.90
    paid_fallback: bool = True
    paid_daily_budget_usd: float | None = None

    def update(self, d: dict) -> None:
        if "switch_threshold" in d:
            v = float(d["switch_threshold"])
            if not 0.0 < v <= 1.0:
                raise ValueError("switch_threshold must be in (0, 1]")
            self.switch_threshold = v
        if "paid_fallback" in d:
            self.paid_fallback = bool(d["paid_fallback"])
        if "paid_daily_budget_usd" in d:
            v = d["paid_daily_budget_usd"]
            self.paid_daily_budget_usd = None if v is None or float(v) < 0 else float(v)


@dataclass
class Binding:
    holder: str
    last_used: float
    pinned: bool = False
    requests: int = 0


class Router:
    def __init__(self, holders: list[HolderRef], *, pool_key: str | None = None,
                 admin_key: str | None = None, internal_key: str | None = None,
                 policy: Policy | None = None, poll_interval: float = 10.0,
                 session_ttl: float = 6 * 3600):
        if not holders:
            raise ValueError("router needs at least one holder")
        self.holders = {h.name: h for h in holders}
        self.pool_key = pool_key
        self.admin_key = admin_key or pool_key
        self.internal_key = internal_key
        self.policy = policy or Policy()
        self.poll_interval = poll_interval
        self.session_ttl = session_ttl
        self.bindings: dict[str, Binding] = {}
        self.events: list[dict] = []  # recent routing decisions, newest last
        self._session: aiohttp.ClientSession | None = None
        self._stop = asyncio.Event()
        self._poller: asyncio.Task | None = None

    # lifecycle -------------------------------------------------------------

    async def _on_startup(self, app: web.Application) -> None:
        self._session = new_client_session()
        self._poller = asyncio.create_task(self._poll_loop())

    async def _on_cleanup(self, app: web.Application) -> None:
        self._stop.set()
        if self._poller:
            await self._poller
        if self._session:
            await self._session.close()

    def app(self) -> web.Application:
        from .mcp import McpServer

        mcp = McpServer(self)
        app = web.Application(client_max_size=64 * 1024 * 1024)
        app.router.add_get("/_ccpool/healthz", self.healthz)
        app.router.add_get("/_ccpool/status", self.h_status)
        app.router.add_get("/_ccpool/sessions/{sid}", self.h_session)
        app.router.add_post("/_ccpool/sessions/{sid}/pin", self.h_pin)
        app.router.add_delete("/_ccpool/sessions/{sid}/pin", self.h_pin)
        app.router.add_get("/_ccpool/policy", self.h_policy)
        app.router.add_post("/_ccpool/policy", self.h_policy)
        app.router.add_post("/_ccpool/holders/{name}/{action:drain|enable}", self.h_drain)
        async def mcp_handler(r: web.Request) -> web.StreamResponse:
            return await mcp.handle_http(r, self._admin_ok(r))

        app.router.add_route("*", "/mcp", mcp_handler)
        app.router.add_route("*", "/{tail:.*}", self.proxy)
        app.on_startup.append(self._on_startup)
        app.on_cleanup.append(self._on_cleanup)
        return app

    # polling ---------------------------------------------------------------

    def _internal_headers(self) -> dict[str, str]:
        return {"x-ccpool-key": self.internal_key} if self.internal_key else {}

    async def poll_once(self) -> None:
        assert self._session is not None

        async def one(h: HolderRef) -> None:
            try:
                async with self._session.get(h.url + "/_ccpool/status", headers=self._internal_headers(),
                                             timeout=aiohttp.ClientTimeout(total=5)) as r:
                    if r.status != 200:
                        raise aiohttp.ClientError(f"HTTP {r.status}")
                    data = json.loads(await r.read())
                q = Quota.from_dict(data.get("quota", {}))
                if q.updated_at >= h.quota.updated_at:
                    h.quota = q
                else:
                    h.quota.cooldown_until = max(h.quota.cooldown_until, q.cooldown_until)
                    h.quota.errors = q.errors
                h.usage = data.get("usage") or {}
                h.backend = data.get("backend") or {}
                if not h.reachable:
                    self._event("holder_up", holder=h.name)
                h.reachable = True
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
                if h.reachable:
                    log.warning("holder %s unreachable: %s", h.name, e)
                    self._event("holder_down", holder=h.name, detail=str(e))
                h.reachable = False

        await asyncio.gather(*(one(h) for h in self.holders.values()))
        cutoff = time.time() - self.session_ttl
        for k in [k for k, b in self.bindings.items() if b.last_used < cutoff]:
            del self.bindings[k]

    async def _poll_loop(self) -> None:
        while not self._stop.is_set():
            await self.poll_once()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_interval)
            except asyncio.TimeoutError:
                pass

    def _event(self, kind: str, **kw) -> None:
        self.events.append({"t": time.time(), "event": kind, **kw})
        del self.events[:-200]

    # selection -------------------------------------------------------------

    @staticmethod
    def session_key(req: web.Request, body: bytes) -> str:
        candidates = [req.headers.get(h) for h in SESSION_HEADERS]
        uid = None
        if body[:1] == b"{":
            try:
                uid = (json.loads(body).get("metadata") or {}).get("user_id")
            except (ValueError, AttributeError):
                uid = None
        for v in candidates + [uid]:
            if isinstance(v, str) and v:
                m = SESSION_RE.search(v) or re.fullmatch(_UUID, v, re.I)
                if m:
                    return "sess:" + (m.group(1) if m.re is SESSION_RE else m.group(0)).lower()
        for v in candidates:
            if v:
                return f"hdr:{v}"
        if isinstance(uid, str) and uid:
            return f"meta:{uid}"
        return f"ip:{req.remote}"

    def paid_spent_today(self) -> float:
        return sum(h.cost_today() for h in self.holders.values() if h.tier == "paid")

    def paid_allowed(self) -> tuple[bool, str]:
        if not self.policy.paid_fallback:
            return False, "paid fallback is off"
        b = self.policy.paid_daily_budget_usd
        if b is not None and self.paid_spent_today() >= b:
            return False, f"paid daily budget ${b:.2f} reached"
        return True, ""

    def ranked(self, tier: str, exclude=frozenset(), now: float | None = None) -> list[HolderRef]:
        now = now or time.time()
        hs = [h for h in self.holders.values() if h.tier == tier and h.usable(now) and h.name not in exclude]
        if tier == "subscription":
            return sorted(hs, key=lambda h: (-h.quota.score(now), h.inflight, h.name))
        return sorted(hs, key=lambda h: (h.priority, h.inflight, h.name))

    def choose(self, key: str, exclude: set[str] = frozenset()) -> tuple[HolderRef | None, str]:
        now = time.time()
        b = self.bindings.get(key)
        seats = self.ranked("subscription", exclude, now)
        paid_ok, why = self.paid_allowed()

        if b and b.holder not in exclude:
            h = self.holders.get(b.holder)
            if h and h.usable(now):
                keep = (
                    b.pinned
                    or (h.tier == "subscription" and h.quota.max_util(now) < self.policy.switch_threshold)
                    or (h.tier == "paid" and not seats and paid_ok)  # stay paid until a seat frees up
                )
                if keep:
                    b.last_used = now
                    b.requests += 1
                    return h, ""

        pick = seats[0] if seats else None
        if pick is None and paid_ok:
            paid = self.ranked("paid", exclude, now)
            pick = paid[0] if paid else None
        if pick is None:
            return None, why or "no backend has quota"
        if not b or b.holder != pick.name:
            self._event("session_moved", session=key, to=pick.name, tier=pick.tier,
                        **({"from": b.holder} if b else {}))
            log.info("session %s -> %s (%s)", key[:48], pick.name, pick.tier)
        self.bindings[key] = Binding(pick.name, now, requests=(b.requests if b else 0) + 1)
        return pick, ""

    # operations shared by the admin API and MCP ------------------------------

    def snapshot(self) -> dict:
        now = time.time()
        return {
            "policy": asdict(self.policy),
            "paid_spent_today_usd": round(self.paid_spent_today(), 4),
            "holders": [
                {
                    "name": h.name, "tier": h.tier, "kind": h.backend.get("kind"), "priority": h.priority,
                    "reachable": h.reachable, "drained": h.drained, "usable": h.usable(now),
                    "inflight": h.inflight, "served": h.served,
                    "quota": h.quota.to_dict(), "usage": h.usage,
                }
                for h in self.holders.values()
            ],
            "sessions": {k: {**asdict(b), "idle_s": int(now - b.last_used)} for k, b in self.bindings.items()},
            "recent_events": self.events[-20:],
        }

    def session_info(self, sid: str) -> dict | None:
        b = self.bindings.get(sid if ":" in sid else f"sess:{sid.lower()}")
        if not b:
            return None
        h = self.holders.get(b.holder)
        return {"backend": b.holder, "tier": h.tier if h else None, "pinned": b.pinned,
                "requests": b.requests, "quota": h.quota.to_dict() if h else None}

    def set_enabled(self, name: str, enabled: bool) -> dict:
        h = self.holders.get(name)
        if not h:
            raise KeyError(name)
        h.drained = not enabled
        if h.drained:
            for k in [k for k, b in self.bindings.items() if b.holder == name]:
                del self.bindings[k]
        self._event("holder_enabled" if enabled else "holder_drained", holder=name)
        return {"name": name, "enabled": enabled}

    def pin_session(self, sid: str, backend: str | None) -> dict:
        key = sid if ":" in sid else f"sess:{sid.lower()}"
        if backend is None:
            b = self.bindings.get(key)
            if b:
                b.pinned = False
            return {"session": key, "pinned": None}
        if backend not in self.holders:
            raise KeyError(backend)
        self.bindings[key] = Binding(backend, time.time(), pinned=True)
        self._event("session_pinned", session=key, to=backend)
        return {"session": key, "pinned": backend}

    # HTTP handlers -----------------------------------------------------------

    def _admin_ok(self, req: web.Request) -> bool:
        return key_ok(presented_key(req), self.admin_key)

    async def healthz(self, req: web.Request) -> web.Response:
        now = time.time()
        n = sum(1 for h in self.holders.values() if h.usable(now))
        return web.json_response({"ok": n > 0, "usable_holders": n}, status=200 if n else 503)

    async def h_status(self, req):
        if not self._admin_ok(req):
            return web.json_response({"error": "unauthorized"}, status=401)
        return web.json_response(self.snapshot())

    async def h_session(self, req):
        # Any pool member may ask which backend their own session is on.
        if not key_ok(presented_key(req), self.pool_key) and not self._admin_ok(req):
            return web.json_response({"error": "unauthorized"}, status=401)
        info = self.session_info(req.match_info["sid"])
        return web.json_response(info or {"error": "unknown session"}, status=200 if info else 404)

    async def h_pin(self, req):
        if not self._admin_ok(req):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            backend = None if req.method == "DELETE" else (await req.json())["backend"]
            return web.json_response(self.pin_session(req.match_info["sid"], backend))
        except (KeyError, ValueError) as e:
            return web.json_response({"error": f"bad request: {e}"}, status=400)

    async def h_policy(self, req):
        if not self._admin_ok(req):
            return web.json_response({"error": "unauthorized"}, status=401)
        if req.method == "POST":
            try:
                self.policy.update(await req.json())
                self._event("policy_changed", policy=asdict(self.policy))
            except (ValueError, TypeError) as e:
                return web.json_response({"error": str(e)}, status=400)
        return web.json_response(asdict(self.policy))

    async def h_drain(self, req):
        if not self._admin_ok(req):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            return web.json_response(self.set_enabled(req.match_info["name"],
                                                      req.match_info["action"] == "enable"))
        except KeyError:
            return web.json_response({"error": "no such holder"}, status=404)

    async def proxy(self, req: web.Request) -> web.StreamResponse:
        if not key_ok(presented_key(req), self.pool_key):
            return web.json_response(
                {"type": "error", "error": {"type": "authentication_error", "message": "bad pool key"}},
                status=401)
        assert self._session is not None
        body = await req.read()
        key = self.session_key(req, body)
        tried: set[str] = set()
        last_error: tuple[int, bytes, dict] | None = None
        why = ""

        while True:
            h, why = self.choose(key, exclude=tried)
            if h is None:
                break
            tried.add(h.name)
            headers = request_headers(req)
            headers.update(self._internal_headers())
            h.inflight += 1
            try:
                try:
                    resp = await self._session.request(req.method, h.url + req.rel_url.path_qs,
                                                       headers=headers, data=body)
                except aiohttp.ClientError as e:
                    log.warning("holder %s failed: %s", h.name, e)
                    h.reachable = False
                    self._event("failover", holder=h.name, reason=str(e))
                    continue
                h.quota.update_from_headers(resp.headers, resp.status)
                if resp.status in FAILOVER_STATUSES:
                    if resp.status in (401, 403):
                        h.quota.errors = max(h.quota.errors, 3)  # credential broken: stop routing here
                    elif resp.status >= 500:
                        h.quota.cooldown_until = max(h.quota.cooldown_until, time.time() + 30)
                    last_error = (resp.status, await resp.read(), dict(resp.headers))
                    resp.release()
                    self._event("failover", holder=h.name, status=resp.status)
                    log.warning("holder %s returned %s, failing over", h.name, resp.status)
                    continue
                h.served += 1
                try:
                    return await stream_back(req, resp)
                finally:
                    resp.release()
            finally:
                h.inflight -= 1

        if last_error and not why.startswith("paid"):
            status, data, hdrs = last_error
            keep = {k: v for k, v in hdrs.items()
                    if k.lower() in ("content-type", "content-encoding", "retry-after")
                    or k.lower().startswith("anthropic-ratelimit-")}
            return web.Response(status=status, body=data, headers=keep)
        now = time.time()
        waits = [h.quota.cooldown_until - now for h in self.holders.values()
                 if h.tier == "subscription" and h.reachable and not h.drained and h.quota.cooldown_until > now]
        retry = str(max(1, int(min(waits)))) if waits else "60"
        return web.json_response(
            {"type": "error", "error": {"type": "rate_limit_error",
                                        "message": f"ccpool: every seat is out of quota ({why})"}},
            status=429, headers={"retry-after": retry})
