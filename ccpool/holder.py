"""Holder: one process per backend (a Team seat, an API key, Bedrock, or LiteLLM).

A subscription holder logs in once and keeps that login alive. Every holder
forwards the requests it receives with its own credential, streams the reply
back, and records quota headers and token usage from it.

Endpoints
  /*                    forwarded to the backend
  GET /_ccpool/status   kind, quota, usage (no secrets)
  GET /_ccpool/healthz  liveness
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

import aiohttp
from aiohttp import web

from .backends import Backend, HttpBackend
from .forward import key_ok, presented_key, request_headers
from .quota import Quota
from .usage import Ledger, UsageMeter

log = logging.getLogger(__name__)


def _request_model(body: bytes) -> str | None:
    if body[:1] != b"{":
        return None
    try:
        m = json.loads(body).get("model")
        return m if isinstance(m, str) else None
    except (ValueError, AttributeError):
        return None


class Holder:
    def __init__(self, name: str, backend: Backend, *, internal_key: str | None = None,
                 ledger: Ledger | None = None):
        self.name = name
        self.backend = backend
        self.internal_key = internal_key
        self.quota = Quota()
        self.ledger = ledger or Ledger()
        self.started_at = time.time()
        self._stop = asyncio.Event()
        self._keepalive: asyncio.Task | None = None

    async def _on_startup(self, app: web.Application) -> None:
        await self.backend.start()
        cred = getattr(self.backend, "cred", None)
        if cred is not None:
            self._keepalive = asyncio.create_task(cred.keepalive(self._stop))

    async def _on_cleanup(self, app: web.Application) -> None:
        self._stop.set()
        if self._keepalive:
            await self._keepalive
        await self.backend.close()

    def app(self) -> web.Application:
        app = web.Application(client_max_size=64 * 1024 * 1024)
        app.router.add_get("/_ccpool/healthz", self.healthz)
        app.router.add_get("/_ccpool/status", self.status)
        app.router.add_route("*", "/{tail:.*}", self.proxy)
        app.on_startup.append(self._on_startup)
        app.on_cleanup.append(self._on_cleanup)
        return app

    async def healthz(self, req: web.Request) -> web.Response:
        return web.json_response({"ok": True, "name": self.name})

    async def status(self, req: web.Request) -> web.Response:
        if not key_ok(presented_key(req), self.internal_key):
            return web.json_response({"error": "unauthorized"}, status=401)
        return web.json_response({
            "name": self.name,
            "uptime_s": int(time.time() - self.started_at),
            "backend": self.backend.describe(),
            "quota": self.quota.to_dict(),
            "usage": self.ledger.summary(),
        })

    async def proxy(self, req: web.Request) -> web.StreamResponse:
        if not key_ok(presented_key(req), self.internal_key):
            return web.json_response({"error": "unauthorized"}, status=401)
        body = await req.read()
        out: web.StreamResponse | None = None
        try:
            async with self.backend.open(req.method, req.rel_url.path_qs, request_headers(req), body) as up:
                self.quota.update_from_headers(up.headers, up.status)
                if up.status == 401:
                    self.quota.errors += 1
                meter = UsageMeter()
                out = web.StreamResponse(status=up.status)
                out.headers.update(up.headers)
                out.headers["x-ccpool-backend"] = self.name
                await out.prepare(req)
                async for chunk in up.chunks:
                    try:
                        meter.feed(chunk)
                    except Exception:  # accounting must never break a stream
                        log.exception("[%s] usage meter failed", self.name)
                        meter = UsageMeter()
                        meter.feed = lambda c: None  # type: ignore[method-assign]
                    await out.write(chunk)
                await out.write_eof()
                if 200 <= up.status < 300:
                    self.ledger.record(meter, _request_model(body))
                return out
        except aiohttp.ClientError as e:
            self.quota.errors += 1
            log.warning("[%s] upstream error: %s", self.name, e)
            if out is not None and out.prepared:
                raise  # already streaming: the client sees a cut connection

            return web.json_response({"error": "upstream_unreachable", "detail": str(e)}, status=502)


def subscription_holder(name, credential, *, upstream=None, internal_key=None) -> Holder:
    """Convenience for the common case (and the tests)."""
    from .backends import ANTHROPIC_API
    return Holder(name, HttpBackend("subscription", upstream=upstream or ANTHROPIC_API,
                                    credential=credential), internal_key=internal_key)
