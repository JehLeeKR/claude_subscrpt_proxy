import json
import time

import aiohttp
import pytest
from aiohttp import web

from ccpool.credentials import OAuthFile, StaticToken
from ccpool.holder import Holder
from ccpool.quota import Quota
from ccpool.router import HolderRef, Router

P = "anthropic-ratelimit-unified-"


def fake_upstream(seats: dict):
    """seats: token -> {"util5": float, "status": int}. Records what it received."""
    seen = []

    async def messages(req: web.Request):
        token = req.headers.get("authorization", "").removeprefix("Bearer ")
        body = await req.json()
        seen.append({"token": token, "beta": req.headers.get("anthropic-beta"), "body": body,
                     "x-api-key": req.headers.get("x-api-key")})
        seat = seats.get(token)
        if seat is None:
            return web.json_response({"error": "invalid token"}, status=401)
        hdrs = {
            P + "status": "rejected" if seat.get("status") == 429 else "allowed",
            P + "5h-utilization": str(seat["util5"]),
            P + "5h-reset": str(int(time.time()) + 3600),
            P + "7d-utilization": "0.1",
            P + "7d-reset": str(int(time.time()) + 86400),
        }
        if seat.get("status") == 429:
            return web.json_response({"error": "rate_limited"}, status=429, headers=hdrs)
        resp = web.StreamResponse(headers={**hdrs, "content-type": "text/event-stream"})
        await resp.prepare(req)
        for i in range(3):
            await resp.write(f"event: delta\ndata: {i}\n\n".encode())
        await resp.write_eof()
        return resp

    app = web.Application()
    app.router.add_post("/v1/messages", messages)
    return app, seen


async def start(aiohttp_server, app):
    s = await aiohttp_server(app)
    return str(s.make_url("")).rstrip("/")


async def post(url, key="pool", body=None, headers=None):
    async with aiohttp.ClientSession() as s:
        async with s.post(url + "/v1/messages",
                          json=body or {"model": "m", "messages": [], "metadata": {"user_id": "sess-1"}},
                          headers={"authorization": f"Bearer {key}", **(headers or {})}) as r:
            return r.status, await r.text(), r.headers


async def build_pool(aiohttp_server, seats, utils):
    up_app, seen = fake_upstream(seats)
    up = await start(aiohttp_server, up_app)
    refs = []
    for name, tok in utils:
        h = Holder(name, StaticToken(tok), upstream=up, internal_key="internal")
        refs.append(HolderRef(name, await start(aiohttp_server, h.app())))
    router = Router(refs, pool_key="pool", internal_key="internal", poll_interval=3600)
    rurl = await start(aiohttp_server, router.app())
    return router, rurl, seen


async def test_holder_swaps_credential_and_streams(aiohttp_server):
    router, url, seen = await build_pool(aiohttp_server, {"tok-a": {"util5": 0.2}}, [("a", "tok-a")])
    status, text, _ = await post(url, headers={"anthropic-beta": "foo", "x-api-key": "client"})
    assert status == 200
    assert text.count("event: delta") == 3
    assert seen[0]["token"] == "tok-a"  # pool key never reaches upstream
    assert seen[0]["x-api-key"] is None
    assert seen[0]["beta"] == "foo,oauth-2025-04-20"
    assert seen[0]["body"]["metadata"]["user_id"] == "sess-1"  # context passed unchanged
    assert router.holders["a"].quota.util_5h == pytest.approx(0.2)


async def test_rejects_bad_pool_key(aiohttp_server):
    _, url, seen = await build_pool(aiohttp_server, {"tok-a": {"util5": 0.2}}, [("a", "tok-a")])
    status, _, _ = await post(url, key="wrong")
    assert status == 401 and not seen


async def test_holder_rejects_direct_access(aiohttp_server):
    up_app, _ = fake_upstream({"tok-a": {"util5": 0}})
    up = await start(aiohttp_server, up_app)
    hurl = await start(aiohttp_server, Holder("a", StaticToken("tok-a"), upstream=up,
                                              internal_key="internal").app())
    status, _, _ = await post(hurl, key="guess")
    assert status == 401


async def test_picks_least_used_and_sticks(aiohttp_server):
    seats = {"tok-a": {"util5": 0.7}, "tok-b": {"util5": 0.1}}
    router, url, seen = await build_pool(aiohttp_server, seats, [("a", "tok-a"), ("b", "tok-b")])
    router.holders["a"].quota.util_5h = 0.7
    router.holders["b"].quota.util_5h = 0.1
    await post(url)
    assert seen[-1]["token"] == "tok-b"
    # b gets busier but stays under the switch threshold: the session stays put.
    seats["tok-b"]["util5"] = 0.8
    await post(url)
    await post(url)
    assert [s["token"] for s in seen] == ["tok-b"] * 3
    # Past the threshold the session moves to the seat with more room.
    seats["tok-b"]["util5"] = 0.95
    await post(url)
    await post(url)
    assert seen[-1]["token"] == "tok-a"


async def test_failover_on_429(aiohttp_server):
    seats = {"tok-a": {"util5": 1.0, "status": 429}, "tok-b": {"util5": 0.5}}
    router, url, seen = await build_pool(aiohttp_server, seats, [("a", "tok-a"), ("b", "tok-b")])
    router.holders["b"].quota.util_5h = 0.5  # a looks better until it answers 429
    status, text, _ = await post(url)
    assert status == 200 and "delta" in text
    assert [s["token"] for s in seen] == ["tok-a", "tok-b"]
    assert not router.holders["a"].quota.available()


async def test_all_exhausted_returns_429_with_retry_after(aiohttp_server):
    seats = {"tok-a": {"util5": 1.0, "status": 429}}
    router, url, _ = await build_pool(aiohttp_server, seats, [("a", "tok-a")])
    status, _, _ = await post(url)
    assert status == 429
    status, _, headers = await post(url)  # now cooling down: answered by the router itself
    assert status == 429 and int(headers["retry-after"]) > 0


async def test_oauth_refresh_on_401_rewrites_file(aiohttp_server, tmp_path):
    refreshed = []

    async def token_ep(req):
        body = await req.json()
        refreshed.append(body)
        return web.json_response({"access_token": "tok-new", "refresh_token": "rt-2", "expires_in": 28800})

    tapp = web.Application()
    tapp.router.add_post("/token", token_ep)
    turl = await start(aiohttp_server, tapp)

    cred_file = tmp_path / "creds.json"
    cred_file.write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "tok-old", "refreshToken": "rt-1",
        "expiresAt": int((time.time() + 7200) * 1000), "subscriptionType": "team",
    }}))
    cred = OAuthFile(cred_file, token_url=turl + "/token", client_id="cid")

    up_app, seen = fake_upstream({"tok-new": {"util5": 0.3}})  # tok-old is no longer valid
    up = await start(aiohttp_server, up_app)
    hurl = await start(aiohttp_server, Holder("a", cred, upstream=up).app())

    status, _, _ = await post(hurl)
    assert status == 200
    assert [s["token"] for s in seen] == ["tok-old", "tok-new"]
    assert refreshed == [{"grant_type": "refresh_token", "refresh_token": "rt-1", "client_id": "cid"}]
    saved = json.loads(cred_file.read_text())["claudeAiOauth"]
    assert saved["accessToken"] == "tok-new" and saved["refreshToken"] == "rt-2"
    assert saved["subscriptionType"] == "team"  # unknown fields survive


def test_score_prefers_expiring_quota():
    now = 1_000_000.0
    soon = Quota(util_5h=0.5, reset_5h=now + 600, util_7d=0.2, reset_7d=now + 86400)
    late = Quota(util_5h=0.5, reset_5h=now + 17000, util_7d=0.2, reset_7d=now + 86400)
    assert soon.score(now) > late.score(now)


def test_window_that_already_reset_counts_as_empty():
    now = 1_000_000.0
    q = Quota(util_5h=0.99, reset_5h=now - 1, util_7d=0.1, reset_7d=now + 86400)
    assert q.max_util(now) == pytest.approx(0.1)


def test_percent_utilization_is_normalized():
    q = Quota()
    q.update_from_headers({P + "5h-utilization": "42"}, 200)
    assert q.util_5h == pytest.approx(0.42)
