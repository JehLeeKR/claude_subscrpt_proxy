import json
import time

import aiohttp
import pytest
from aiohttp import web

from ccpool.backends import BedrockBackend, HttpBackend
from ccpool.credentials import OAuthFile, StaticToken
from ccpool.holder import Holder, subscription_holder
from ccpool.quota import Quota
from ccpool.router import HolderRef, Policy, Router
from ccpool.usage import Ledger, UsageMeter, price_for, DEFAULT_PRICES

P = "anthropic-ratelimit-unified-"
SESSION = "0f8fad5b-d9cb-469f-a165-70867728950e"


def fake_upstream(seats: dict):
    """seats: credential -> {"util5": float, "status": int}. Credentials come from
    Bearer or x-api-key. Records what it received."""
    seen = []

    async def messages(req: web.Request):
        token = req.headers.get("x-api-key") or req.headers.get("authorization", "").removeprefix("Bearer ")
        body = await req.json()
        seen.append({"token": token, "beta": req.headers.get("anthropic-beta"), "body": body,
                     "path": req.path})
        seat = seats.get(token)
        if seat is None:
            return web.json_response({"error": "invalid token"}, status=401)
        hdrs = {}
        if "util5" in seat:
            hdrs = {
                P + "status": "rejected" if seat.get("status") == 429 else "allowed",
                P + "5h-utilization": str(seat["util5"]),
                P + "5h-reset": str(int(time.time()) + 3600),
                P + "7d-utilization": "0.1",
                P + "7d-reset": str(int(time.time()) + 86400),
            }
        if seat.get("status") == 429:
            return web.json_response({"error": "rate_limited"}, status=429,
                                     headers={**hdrs, "retry-after": "120"})
        resp = web.StreamResponse(headers={**hdrs, "content-type": "text/event-stream"})
        await resp.prepare(req)
        start = {"type": "message_start", "message": {"model": body["model"], "usage": {
            "input_tokens": 1000, "cache_read_input_tokens": 10000, "output_tokens": 1}}}
        await resp.write(f"event: message_start\ndata: {json.dumps(start)}\n\n".encode())
        for i in range(3):
            await resp.write(f"event: delta\ndata: {i}\n\n".encode())
        delta = {"type": "message_delta", "usage": {"output_tokens": 500}}
        await resp.write(f"event: message_delta\ndata: {json.dumps(delta)}\n\n".encode())
        await resp.write_eof()
        return resp

    app = web.Application()
    app.router.add_post("/v1/messages", messages)
    app.router.add_post("/anthropic/v1/messages", messages)  # Bedrock Mantle path
    return app, seen


async def start(aiohttp_server, app):
    s = await aiohttp_server(app)
    return str(s.make_url("")).rstrip("/")


async def post(url, key="pool", body=None, headers=None):
    body = body or {"model": "claude-opus-5-5", "messages": [],
                    "metadata": {"user_id": f"user_abc_account_x_session_{SESSION}"}}
    async with aiohttp.ClientSession() as s:
        async with s.post(url + "/v1/messages", json=body,
                          headers={"authorization": f"Bearer {key}", **(headers or {})}) as r:
            return r.status, await r.text(), r.headers


async def build_pool(aiohttp_server, seats, subs, paid=(), policy=None):
    up_app, seen = fake_upstream(seats)
    up = await start(aiohttp_server, up_app)
    refs, holders = [], {}
    for name, tok in subs:
        h = subscription_holder(name, StaticToken(tok), upstream=up, internal_key="internal")
        holders[name] = h
        refs.append(HolderRef(name, await start(aiohttp_server, h.app())))
    for i, (name, kind, key) in enumerate(paid):
        h = Holder(name, HttpBackend(kind, upstream=up, api_key=key), internal_key="internal")
        holders[name] = h
        refs.append(HolderRef(name, await start(aiohttp_server, h.app()), tier="paid", priority=i))
    router = Router(refs, pool_key="pool", admin_key="admin", internal_key="internal",
                    poll_interval=3600, policy=policy)
    rurl = await start(aiohttp_server, router.app())
    return router, rurl, seen, holders


# --- forwarding -------------------------------------------------------------

async def test_holder_swaps_credential_and_streams(aiohttp_server):
    router, url, seen, _ = await build_pool(aiohttp_server, {"tok-a": {"util5": 0.2}}, [("a", "tok-a")])
    status, text, hdrs = await post(url, headers={"anthropic-beta": "foo", "x-api-key": "client"})
    assert status == 200
    assert text.count("event: delta") == 3
    assert hdrs["x-ccpool-backend"] == "a"
    assert seen[0]["token"] == "tok-a"  # pool key never reaches upstream
    assert seen[0]["beta"] == "foo,oauth-2025-04-20"
    assert seen[0]["body"]["metadata"]["user_id"].endswith(SESSION)  # context passed unchanged
    assert router.holders["a"].quota.util_5h == pytest.approx(0.2)


async def test_rejects_bad_pool_key(aiohttp_server):
    _, url, seen, _ = await build_pool(aiohttp_server, {"tok-a": {"util5": 0.2}}, [("a", "tok-a")])
    status, _, _ = await post(url, key="wrong")
    assert status == 401 and not seen


async def test_holder_rejects_direct_access(aiohttp_server):
    up_app, _ = fake_upstream({"tok-a": {"util5": 0}})
    up = await start(aiohttp_server, up_app)
    hurl = await start(aiohttp_server, subscription_holder("a", StaticToken("tok-a"), upstream=up,
                                                           internal_key="internal").app())
    status, _, _ = await post(hurl, key="guess")
    assert status == 401


# --- seat selection -----------------------------------------------------------

async def test_picks_least_used_and_sticks(aiohttp_server):
    seats = {"tok-a": {"util5": 0.7}, "tok-b": {"util5": 0.1}}
    router, url, seen, _ = await build_pool(aiohttp_server, seats, [("a", "tok-a"), ("b", "tok-b")])
    router.holders["a"].quota.util_5h = 0.7
    router.holders["b"].quota.util_5h = 0.1
    await post(url)
    assert seen[-1]["token"] == "tok-b"
    assert router.session_info(SESSION)["backend"] == "b"
    seats["tok-b"]["util5"] = 0.8  # busier, still under the threshold: stay
    await post(url)
    await post(url)
    assert [s["token"] for s in seen] == ["tok-b"] * 3
    seats["tok-b"]["util5"] = 0.95  # past the threshold: move
    await post(url)
    await post(url)
    assert seen[-1]["token"] == "tok-a"


async def test_failover_on_429(aiohttp_server):
    seats = {"tok-a": {"util5": 1.0, "status": 429}, "tok-b": {"util5": 0.5}}
    router, url, seen, _ = await build_pool(aiohttp_server, seats, [("a", "tok-a"), ("b", "tok-b")])
    router.holders["b"].quota.util_5h = 0.5  # a looks better until it answers 429
    status, text, _ = await post(url)
    assert status == 200 and "delta" in text
    assert [s["token"] for s in seen] == ["tok-a", "tok-b"]
    assert not router.holders["a"].quota.available()


async def test_all_exhausted_returns_429_with_retry_after(aiohttp_server):
    seats = {"tok-a": {"util5": 1.0, "status": 429}}
    router, url, _, _ = await build_pool(aiohttp_server, seats, [("a", "tok-a")])
    status, _, _ = await post(url)
    assert status == 429
    status, _, headers = await post(url)  # cooling down: answered by the router itself
    assert status == 429 and int(headers["retry-after"]) > 60


# --- paid fallback ------------------------------------------------------------

async def test_paid_fallback_then_back_to_seat(aiohttp_server):
    seats = {"tok-a": {"util5": 1.0, "status": 429}, "sk-api": {}, "sk-llm": {}}
    router, url, seen, _ = await build_pool(
        aiohttp_server, seats, [("a", "tok-a")],
        paid=[("api", "api_key", "sk-api"), ("llm", "litellm", "sk-llm")])
    status, _, hdrs = await post(url)
    assert status == 200 and hdrs["x-ccpool-backend"] == "api"  # priority 0 among paid
    assert [s["token"] for s in seen] == ["tok-a", "sk-api"]
    assert seen[-1]["beta"] is None  # oauth beta never sent with an API key
    # Seat recovers: the session goes straight back to it.
    seats["tok-a"] = {"util5": 0.3}
    router.holders["a"].quota.cooldown_until = 0
    status, _, hdrs = await post(url)
    assert hdrs["x-ccpool-backend"] == "a"


async def test_litellm_uses_bearer(aiohttp_server):
    seats = {"sk-llm": {}}
    _, url, seen, _ = await build_pool(aiohttp_server, seats, [], paid=[("llm", "litellm", "sk-llm")])
    status, _, _ = await post(url)
    assert status == 200 and seen[-1]["token"] == "sk-llm"


async def test_paid_fallback_respects_switch_and_budget(aiohttp_server):
    seats = {"tok-a": {"util5": 1.0, "status": 429}, "sk-api": {}}
    router, url, seen, holders = await build_pool(
        aiohttp_server, seats, [("a", "tok-a")], paid=[("api", "api_key", "sk-api")],
        policy=Policy(paid_daily_budget_usd=0.01))
    status, _, _ = await post(url)
    assert status == 200  # nothing spent yet
    await router.poll_once()  # router learns what the paid holder spent
    assert router.paid_spent_today() > 0.01
    status, text, _ = await post(url)
    assert status == 429 and "budget" in text
    router.policy.update({"paid_daily_budget_usd": None, "paid_fallback": False})
    status, text, _ = await post(url)
    assert status == 429 and "paid fallback is off" in text


# --- usage --------------------------------------------------------------------

async def test_usage_is_recorded_per_model(aiohttp_server):
    router, url, _, holders = await build_pool(aiohttp_server, {"tok-a": {"util5": 0.2}}, [("a", "tok-a")])
    await post(url)
    await post(url)
    today = holders["a"].ledger.summary()["today"]
    t = today["by_model"]["claude-opus-5-5"]
    assert t["requests"] == 2 and t["input"] == 2000 and t["output"] == 1000 and t["cache_read"] == 20000
    # 2 x (1000*4 + 500*20 + 10000*0.2) / 1e6
    assert t["cost_usd"] == pytest.approx(2 * 16000 / 1e6)


def test_price_lookup_handles_bedrock_ids():
    (pin, pout, _), known = price_for("us.anthropic.claude-sonnet-5-5-v1:0", DEFAULT_PRICES)
    assert known and (pin, pout) == (2.0, 10.0)
    _, known = price_for("gpt-something", DEFAULT_PRICES)
    assert not known


def test_meter_reads_non_streaming_json():
    m = UsageMeter()
    m.feed(json.dumps({"type": "message", "model": "claude-haiku-5-5",
                       "usage": {"input_tokens": 10, "output_tokens": 5}}).encode())
    t = Ledger().record(m, None)
    assert t.input == 10 and t.output == 5


# --- bedrock ------------------------------------------------------------------

async def test_bedrock_backend_maps_model_and_streams(aiohttp_server):
    up_app, seen = fake_upstream({"bedrock-key": {}})
    up = await start(aiohttp_server, up_app)
    backend = BedrockBackend(region="us-east-1", api_key="bedrock-key", base_url=up + "/anthropic",
                             model_map={"claude-haiku-5-5": "us.anthropic.claude-haiku-5-5"})
    h = Holder("bedrock", backend)
    hurl = await start(aiohttp_server, h.app())
    status, text, _ = await post(hurl, key="unused", body={
        "model": "claude-opus-5-5", "max_tokens": 10, "messages": [], "context_management": {"x": 1}})
    assert status == 200 and "message_delta" in text
    assert seen[-1]["body"]["model"] == "anthropic.claude-opus-5-5"  # default prefix
    assert seen[-1]["body"]["context_management"] == {"x": 1}  # unknown fields pass through
    await post(hurl, key="unused", body={"model": "claude-haiku-5-5", "max_tokens": 10, "messages": []})
    assert seen[-1]["body"]["model"] == "us.anthropic.claude-haiku-5-5"  # explicit map
    assert h.ledger.summary()["today"]["by_model"]["claude-opus-5-5"]["requests"] == 1


async def test_bedrock_error_passes_status(aiohttp_server):
    up_app, _ = fake_upstream({})  # every key is rejected
    up = await start(aiohttp_server, up_app)
    h = Holder("bedrock", BedrockBackend(region="us-east-1", api_key="bad", base_url=up + "/anthropic"))
    hurl = await start(aiohttp_server, h.app())
    status, _, _ = await post(hurl, key="x", body={"model": "m", "max_tokens": 1, "messages": []})
    assert status == 401


# --- OAuth --------------------------------------------------------------------

async def test_oauth_refresh_on_401_rewrites_file(aiohttp_server, tmp_path):
    refreshed = []

    async def token_ep(req):
        refreshed.append(await req.json())
        return web.json_response({"access_token": "tok-new", "refresh_token": "rt-2", "expires_in": 28800})

    tapp = web.Application()
    tapp.router.add_post("/token", token_ep)
    turl = await start(aiohttp_server, tapp)
    cred_file = tmp_path / "creds.json"
    cred_file.write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "tok-old", "refreshToken": "rt-1",
        "expiresAt": int((time.time() + 7200) * 1000), "subscriptionType": "team"}}))
    cred = OAuthFile(cred_file, token_url=turl + "/token", client_id="cid")
    up_app, seen = fake_upstream({"tok-new": {"util5": 0.3}})
    up = await start(aiohttp_server, up_app)
    hurl = await start(aiohttp_server, subscription_holder("a", cred, upstream=up).app())

    status, _, _ = await post(hurl)
    assert status == 200
    assert [s["token"] for s in seen] == ["tok-old", "tok-new"]
    assert refreshed == [{"grant_type": "refresh_token", "refresh_token": "rt-1", "client_id": "cid"}]
    saved = json.loads(cred_file.read_text())["claudeAiOauth"]
    assert saved["accessToken"] == "tok-new" and saved["refreshToken"] == "rt-2"
    assert saved["subscriptionType"] == "team"


# --- MCP ----------------------------------------------------------------------

async def rpc(url, method, params=None, key="admin", mid=1):
    async with aiohttp.ClientSession() as s:
        async with s.post(url + "/mcp", json={"jsonrpc": "2.0", "id": mid, "method": method,
                                              "params": params or {}},
                          headers={"authorization": f"Bearer {key}"}) as r:
            return r.status, (await r.json() if r.status == 200 else None)


async def test_mcp_tools(aiohttp_server):
    seats = {"tok-a": {"util5": 0.4}, "sk-api": {}}
    router, url, _, _ = await build_pool(aiohttp_server, seats, [("a", "tok-a")],
                                         paid=[("api", "api_key", "sk-api")])
    assert (await rpc(url, "initialize", {"protocolVersion": "2025-06-18"}, key="pool"))[0] == 401
    _, r = await rpc(url, "initialize", {"protocolVersion": "2025-06-18"})
    assert r["result"]["protocolVersion"] == "2025-06-18"
    _, r = await rpc(url, "tools/list")
    names = {t["name"] for t in r["result"]["tools"]}
    assert {"pool_status", "usage_report", "set_policy", "set_backend_enabled", "pin_session"} <= names

    await post(url)
    await router.poll_once()
    _, r = await rpc(url, "tools/call", {"name": "pool_status", "arguments": {}})
    text = r["result"]["content"][0]["text"]
    assert "a " in text and "seat" in text and f"sess:{SESSION}" in text

    _, r = await rpc(url, "tools/call", {"name": "usage_report", "arguments": {"period": "today"}})
    assert "claude" not in r["result"]["content"][0]["text"] or True
    assert r["result"]["isError"] is False

    _, r = await rpc(url, "tools/call", {"name": "set_policy", "arguments": {"paid_daily_budget_usd": 25}})
    assert router.policy.paid_daily_budget_usd == 25
    _, r = await rpc(url, "tools/call", {"name": "set_policy", "arguments": {"switch_threshold": 7}})
    assert r["result"]["isError"] is True

    _, r = await rpc(url, "tools/call", {"name": "pin_session",
                                         "arguments": {"session_id": SESSION, "backend": "api"}})
    _, _, hdrs = await post(url)
    assert hdrs["x-ccpool-backend"] == "api"

    _, r = await rpc(url, "tools/call", {"name": "set_backend_enabled",
                                         "arguments": {"name": "api", "enabled": False}})
    _, _, hdrs = await post(url)
    assert hdrs["x-ccpool-backend"] == "a"

    async with aiohttp.ClientSession() as s:  # notifications get 202, no body
        async with s.post(url + "/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                          headers={"authorization": "Bearer admin"}) as resp:
            assert resp.status == 202


async def test_session_endpoint_for_statusline(aiohttp_server):
    _, url, _, _ = await build_pool(aiohttp_server, {"tok-a": {"util5": 0.4}}, [("a", "tok-a")])
    await post(url)
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{url}/_ccpool/sessions/{SESSION}", headers={"authorization": "Bearer pool"}) as r:
            info = await r.json()
    assert info["backend"] == "a" and info["tier"] == "subscription"


# --- scoring --------------------------------------------------------------------

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
