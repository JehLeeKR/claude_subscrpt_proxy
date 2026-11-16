"""Shared HTTP forwarding helpers: header filtering and streaming a response back."""

from __future__ import annotations

import hmac

import aiohttp
from aiohttp import web

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}
# Credentials that must never cross a hop.
AUTH_HEADERS = {"authorization", "x-api-key", "x-ccpool-key"}


def request_headers(req: web.Request) -> dict[str, str]:
    return {
        k: v for k, v in req.headers.items()
        if k.lower() not in HOP_BY_HOP and k.lower() not in AUTH_HEADERS
    }


def response_headers(resp: aiohttp.ClientResponse) -> dict[str, str]:
    # content-encoding is kept: the body is passed through still compressed.
    return {k: v for k, v in resp.headers.items() if k.lower() not in HOP_BY_HOP}


async def stream_back(req: web.Request, upstream: aiohttp.ClientResponse) -> web.StreamResponse:
    out = web.StreamResponse(status=upstream.status, reason=upstream.reason)
    out.headers.update(response_headers(upstream))
    await out.prepare(req)
    async for chunk in upstream.content.iter_any():
        await out.write(chunk)
    await out.write_eof()
    return out


def presented_key(req: web.Request) -> str | None:
    auth = req.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return req.headers.get("x-api-key") or req.headers.get("x-ccpool-key")


def key_ok(presented: str | None, expected: str | None) -> bool:
    if not expected:
        return True
    return presented is not None and hmac.compare_digest(presented, expected)


def new_client_session() -> aiohttp.ClientSession:
    return aiohttp.ClientSession(
        auto_decompress=False,
        timeout=aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=600),
    )
