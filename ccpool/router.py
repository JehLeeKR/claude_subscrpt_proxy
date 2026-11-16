"""Router: the single endpoint Claude Code talks to.

Clients set ``ANTHROPIC_BASE_URL`` to the router and ``ANTHROPIC_AUTH_TOKEN``
to the pool key. For each request the router picks a holder by quota, keeps a
Claude Code session on the same holder while it has room, and fails over to the
next holder when one is rate limited or its login breaks.

Endpoints
  /*                    forwarded to the chosen holder
  GET /_ccpool/status   all holders, their quota, and session assignments
  GET /_ccpool/healthz  liveness
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field

import aiohttp
from aiohttp import web

from .forward import key_ok, new_client_session, presented_key, request_headers, stream_back
from .quota import Quota

log = logging.getLogger(__name__)

FAILOVER_STATUSES = {401, 429, 502, 503}
SESSION_HEADERS = ("x-claude-code-session-id", "x-session-id")


@dataclass
class HolderRef:
    name: str
    url: str
    quota: Quota = field(default_factory=Quota)
    reachable: bool = True
    drained: bool = False
    inflight: int = 0
    served: int = 0

    def usable(self, now: float) -> bool:
        return self.reachable and not self.drained and self.quota.available(now)


@dataclass
class Binding:
    holder: str
    last_used: float


class Router:
    def __init__(
        self,
        holders: list[HolderRef],
        *,
        pool_key: str | None = None,
        internal_key: str | None = None,
        switch_threshold: float = 0.90,
        poll_interval: float = 15.0,
        session_ttl: float = 6 * 3600,
    ):
        if not holders:
            raise ValueError("router needs at least one holder")
        self.holders = {h.name: h for h in holders}
        self.pool_key = pool_key
        self.internal_key = internal_key
        self.switch_threshold = switch_threshold
        self.poll_interval = poll_interval
        self.session_ttl = session_ttl
        self.bindings: dict[str, Binding] = {}
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
        app = web.Application(client_max_size=64 * 1024 * 1024)
        app.router.add_get("/_ccpool/healthz", self.healthz)
        app.router.add_get("/_ccpool/status", self.status)
        app.router.add_post("/_ccpool/holders/{name}/drain", self.set_drain)
        app.router.add_post("/_ccpool/holders/{name}/enable", self.set_drain)
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
                async with self._session.get(
                    h.url + "/_ccpool/status",
                    headers=self._internal_headers(),
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as r:
                    if r.status != 200:
                        raise aiohttp.ClientError(f"HTTP {r.status}")
                    data = json.loads(await r.read())
                q = Quota.from_dict(data.get("quota", {}))
                # Keep the newer of the two views; the router sees headers too.
                if q.updated_at >= h.quota.updated_at:
                    h.quota = q
                else:
                    h.quota.cooldown_until = max(h.quota.cooldown_until, q.cooldown_until)
                    h.quota.errors = q.errors
                h.reachable = True
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
                if h.reachable:
                    log.warning("holder %s unreachable: %s", h.name, e)
                h.reachable = False

        await asyncio.gather(*(one(h) for h in self.holders.values()))
        self._expire_bindings()

    async def _poll_loop(self) -> None:
        while not self._stop.is_set():
            await self.poll_once()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_interval)
            except asyncio.TimeoutError:
                pass

    def _expire_bindings(self) -> None:
        cutoff = time.time() - self.session_ttl
        for k in [k for k, b in self.bindings.items() if b.last_used < cutoff]:
            del self.bindings[k]

    # selection -------------------------------------------------------------

    @staticmethod
    def session_key(req: web.Request, body: bytes) -> str:
        for h in SESSION_HEADERS:
            v = req.headers.get(h)
            if v:
                return f"hdr:{v}"
        if body[:1] == b"{":
            try:
                uid = (json.loads(body).get("metadata") or {}).get("user_id")
                if uid:
                    return f"meta:{uid}"
            except (ValueError, AttributeError):
                pass
        return f"ip:{req.remote}"

    def ranked(self, now: float | None = None) -> list[HolderRef]:
        now = now or time.time()
        usable = [h for h in self.holders.values() if h.usable(now)]
        # Ties go to the holder with less work in flight.
        return sorted(usable, key=lambda h: (-h.quota.score(now), h.inflight, h.name))

    def choose(self, key: str, exclude: set[str] = frozenset()) -> HolderRef | None:
        now = time.time()
        b = self.bindings.get(key)
        if b and b.holder not in exclude:
            h = self.holders.get(b.holder)
            if h and h.usable(now) and h.quota.max_util(now) < self.switch_threshold:
                b.last_used = now
                return h
        for h in self.ranked(now):
            if h.name not in exclude:
                if not b or b.holder != h.name:
                    log.info("session %s -> %s", key[:40], h.name)
                self.bindings[key] = Binding(h.name, now)
                return h
        return None

    # handlers --------------------------------------------------------------

    async def healthz(self, req: web.Request) -> web.Response:
        now = time.time()
        n = sum(1 for h in self.holders.values() if h.usable(now))
        return web.json_response({"ok": n > 0, "usable_holders": n}, status=200 if n else 503)

    async def status(self, req: web.Request) -> web.Response:
        if not key_ok(presented_key(req), self.pool_key):
            return web.json_response({"error": "unauthorized"}, status=401)
        now = time.time()
        return web.json_response({
            "holders": [
                {
                    "name": h.name, "url": h.url, "reachable": h.reachable, "drained": h.drained,
                    "usable": h.usable(now), "inflight": h.inflight, "served": h.served,
                    "quota": h.quota.to_dict(),
                }
                for h in self.holders.values()
            ],
            "sessions": {k: {"holder": b.holder, "idle_s": int(now - b.last_used)}
                         for k, b in self.bindings.items()},
        })

    async def set_drain(self, req: web.Request) -> web.Response:
        if not key_ok(presented_key(req), self.pool_key):
            return web.json_response({"error": "unauthorized"}, status=401)
        h = self.holders.get(req.match_info["name"])
        if not h:
            return web.json_response({"error": "no such holder"}, status=404)
        h.drained = req.path.endswith("/drain")
        if h.drained:
            for k in [k for k, b in self.bindings.items() if b.holder == h.name]:
                del self.bindings[k]
        return web.json_response({"name": h.name, "drained": h.drained})

    async def proxy(self, req: web.Request) -> web.StreamResponse:
        if not key_ok(presented_key(req), self.pool_key):
            return web.json_response(
                {"type": "error", "error": {"type": "authentication_error", "message": "bad pool key"}},
                status=401,
            )
        assert self._session is not None
        body = await req.read()
        key = self.session_key(req, body)
        tried: set[str] = set()
        last_error: tuple[int, bytes, dict] | None = None

        while True:
            h = self.choose(key, exclude=tried)
            if h is None:
                break
            tried.add(h.name)
            headers = request_headers(req)
            headers.update(self._internal_headers())
            h.inflight += 1
            try:
                try:
                    resp = await self._session.request(
                        req.method, h.url + req.rel_url.path_qs, headers=headers, data=body
                    )
                except aiohttp.ClientError as e:
                    log.warning("holder %s failed: %s", h.name, e)
                    h.reachable = False
                    continue

                h.quota.update_from_headers(resp.headers, resp.status)
                if resp.status in FAILOVER_STATUSES:
                    if resp.status == 401:
                        h.quota.errors = max(h.quota.errors, 3)  # login broken: stop routing here
                    last_error = (resp.status, await resp.read(), dict(resp.headers))
                    resp.release()
                    log.warning("holder %s returned %s, failing over", h.name, resp.status)
                    continue

                h.served += 1
                try:
                    return await stream_back(req, resp)
                finally:
                    resp.release()
            finally:
                h.inflight -= 1

        if last_error:
            status, data, hdrs = last_error
            keep = {k: v for k, v in hdrs.items()
                    if k.lower() in ("content-type", "content-encoding", "retry-after")
                    or k.lower().startswith("anthropic-ratelimit-")}
            return web.Response(status=status, body=data, headers=keep)
        # Every holder is cooling down: tell the client when the first one frees up.
        now = time.time()
        waits = [h.quota.cooldown_until - now for h in self.holders.values()
                 if h.reachable and not h.drained and h.quota.cooldown_until > now]
        retry = str(max(1, int(min(waits)))) if waits else "60"
        return web.json_response(
            {"type": "error", "error": {"type": "rate_limit_error",
                                        "message": "ccpool: every seat is out of quota"}},
            status=429, headers={"retry-after": retry},
        )
