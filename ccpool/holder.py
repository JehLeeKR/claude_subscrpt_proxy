"""Holder: one process per Team seat.

It owns one login, keeps it alive, and forwards the requests it receives to the
Anthropic API with that seat's token. The request body (Claude Code's context)
is passed through unchanged; only the credential is attached here.

Endpoints
  /*                    forwarded upstream with this seat's token
  GET /_ccpool/status   quota and login state (no secrets)
  GET /_ccpool/healthz  liveness
"""

from __future__ import annotations

import asyncio
import logging
import time

import aiohttp
from aiohttp import web

from .credentials import Credential
from .forward import key_ok, new_client_session, presented_key, request_headers, stream_back
from .quota import Quota

log = logging.getLogger(__name__)

OAUTH_BETA = "oauth-2025-04-20"
DEFAULT_UPSTREAM = "https://api.anthropic.com"


def _merge_beta(existing: str | None) -> str:
    parts = [p.strip() for p in (existing or "").split(",") if p.strip()]
    if OAUTH_BETA not in parts:
        parts.append(OAUTH_BETA)
    return ",".join(parts)


class Holder:
    def __init__(
        self,
        name: str,
        credential: Credential,
        *,
        upstream: str = DEFAULT_UPSTREAM,
        internal_key: str | None = None,
    ):
        self.name = name
        self.cred = credential
        self.upstream = upstream.rstrip("/")
        self.internal_key = internal_key
        self.quota = Quota()
        self.started_at = time.time()
        self.requests = 0
        self._session: aiohttp.ClientSession | None = None
        self._stop = asyncio.Event()
        self._keepalive: asyncio.Task | None = None

    # lifecycle -------------------------------------------------------------

    async def _on_startup(self, app: web.Application) -> None:
        self._session = new_client_session()
        self._keepalive = asyncio.create_task(self.cred.keepalive(self._stop))

    async def _on_cleanup(self, app: web.Application) -> None:
        self._stop.set()
        if self._keepalive:
            await self._keepalive
        if self._session:
            await self._session.close()

    def app(self) -> web.Application:
        app = web.Application(client_max_size=64 * 1024 * 1024)
        app.router.add_get("/_ccpool/healthz", self.healthz)
        app.router.add_get("/_ccpool/status", self.status)
        app.router.add_route("*", "/{tail:.*}", self.proxy)
        app.on_startup.append(self._on_startup)
        app.on_cleanup.append(self._on_cleanup)
        return app

    # handlers --------------------------------------------------------------

    async def healthz(self, req: web.Request) -> web.Response:
        return web.json_response({"ok": True, "name": self.name})

    async def status(self, req: web.Request) -> web.Response:
        if not key_ok(presented_key(req), self.internal_key):
            return web.json_response({"error": "unauthorized"}, status=401)
        return web.json_response({
            "name": self.name,
            "requests": self.requests,
            "uptime_s": int(time.time() - self.started_at),
            "credential": self.cred.describe(),
            "quota": self.quota.to_dict(),
        })

    async def proxy(self, req: web.Request) -> web.StreamResponse:
        if not key_ok(presented_key(req), self.internal_key):
            return web.json_response({"error": "unauthorized"}, status=401)
        assert self._session is not None
        body = await req.read()
        url = self.upstream + req.rel_url.path_qs
        self.requests += 1

        for attempt in (0, 1):
            headers = request_headers(req)
            headers["authorization"] = f"Bearer {await self.cred.token()}"
            headers["anthropic-beta"] = _merge_beta(req.headers.get("anthropic-beta"))
            try:
                upstream = await self._session.request(req.method, url, headers=headers, data=body)
            except aiohttp.ClientError as e:
                self.quota.errors += 1
                log.warning("[%s] upstream error: %s", self.name, e)
                return web.json_response({"error": "upstream_unreachable", "detail": str(e)}, status=502)

            self.quota.update_from_headers(upstream.headers, upstream.status)

            if upstream.status == 401 and attempt == 0:
                await upstream.release()
                log.warning("[%s] 401 from upstream, refreshing login", self.name)
                if await self.cred.force_refresh():
                    continue
                self.quota.errors += 1
                return web.json_response({"error": "login_invalid", "holder": self.name}, status=401)

            if upstream.status == 401:
                self.quota.errors += 1
            try:
                return await stream_back(req, upstream)
            finally:
                upstream.release()
        raise AssertionError("unreachable")
