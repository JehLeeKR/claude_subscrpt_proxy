"""Upstream backends a holder can front.

kind           tier          auth                                   upstream
subscription   subscription  Team seat OAuth login (kept refreshed)  api.anthropic.com
api_key        paid          Console API key (x-api-key)             api.anthropic.com
litellm        paid          LiteLLM virtual key (Bearer)            your LiteLLM proxy's /v1/messages
bedrock        paid          AWS credentials (SigV4) or Bedrock key  Bedrock Mantle Messages endpoint

Every backend speaks the Anthropic Messages API, so Claude Code's request body
passes through unchanged except for an optional model-name mapping.
"""

from __future__ import annotations

import contextlib
import inspect
import json
import logging
from typing import AsyncIterator

import aiohttp

from .credentials import Credential
from .forward import HOP_BY_HOP, new_client_session

log = logging.getLogger(__name__)

OAUTH_BETA = "oauth-2025-04-20"
ANTHROPIC_API = "https://api.anthropic.com"
TIERS = {"subscription": "subscription", "api_key": "paid", "litellm": "paid", "bedrock": "paid"}


class Upstream:
    """A response being streamed back from a backend."""

    def __init__(self, status: int, headers: dict[str, str], chunks: AsyncIterator[bytes]):
        self.status = status
        self.headers = headers
        self.chunks = chunks

    async def read(self) -> bytes:
        return b"".join([c async for c in self.chunks])


def _merge_beta(existing: str | None, add: str | None = None, remove: str | None = None) -> str:
    parts = [p.strip() for p in (existing or "").split(",") if p.strip()]
    if add and add not in parts:
        parts.append(add)
    if remove:
        parts = [p for p in parts if p != remove]
    return ",".join(parts)


def map_model(body: bytes, model_map: dict[str, str], default_prefix: str = "") -> tuple[bytes, str | None]:
    """Rewrite body["model"] through the map. Returns (body, original model)."""
    if body[:1] != b"{":
        return body, None
    try:
        data = json.loads(body)
    except ValueError:
        return body, None
    model = data.get("model")
    if not isinstance(model, str):
        return body, None
    target = model_map.get(model)
    if target is None and default_prefix and not model.startswith(default_prefix):
        target = default_prefix + model
    if target is None or target == model:
        return body, model
    data["model"] = target
    return json.dumps(data, separators=(",", ":")).encode(), model


class Backend:
    kind = "base"

    def __init__(self, model_map: dict[str, str] | None = None):
        self.model_map = model_map or {}

    @property
    def tier(self) -> str:
        return TIERS[self.kind]

    async def start(self) -> None: ...
    async def close(self) -> None: ...

    def describe(self) -> dict:
        return {"kind": self.kind, "tier": self.tier, "model_map": self.model_map}

    def open(self, method: str, path_qs: str, headers: dict[str, str], body: bytes
             ) -> contextlib.AbstractAsyncContextManager[Upstream]:
        raise NotImplementedError


class HttpBackend(Backend):
    """subscription / api_key / litellm: plain HTTP to a Messages-compatible upstream."""

    def __init__(self, kind: str, *, upstream: str, credential: Credential | None = None,
                 api_key: str | None = None, model_map: dict[str, str] | None = None):
        super().__init__(model_map)
        if kind not in ("subscription", "api_key", "litellm"):
            raise ValueError(kind)
        if kind == "subscription" and credential is None:
            raise ValueError("subscription backend needs a credential")
        if kind != "subscription" and not api_key:
            raise ValueError(f"{kind} backend needs a key")
        self.kind = kind
        self.upstream = upstream.rstrip("/")
        self.cred = credential
        self.api_key = api_key
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        self._session = new_client_session()

    async def close(self) -> None:
        if self._session:
            await self._session.close()

    def describe(self) -> dict:
        d = super().describe() | {"upstream": self.upstream}
        if self.cred:
            d["credential"] = self.cred.describe()
        return d

    async def _auth(self, headers: dict[str, str]) -> None:
        if self.kind == "subscription":
            headers["authorization"] = f"Bearer {await self.cred.token()}"
            headers["anthropic-beta"] = _merge_beta(headers.get("anthropic-beta"), add=OAUTH_BETA)
        else:
            beta = _merge_beta(headers.get("anthropic-beta"), remove=OAUTH_BETA)
            if beta:
                headers["anthropic-beta"] = beta
            else:
                headers.pop("anthropic-beta", None)
            if self.kind == "api_key":
                headers["x-api-key"] = self.api_key
            else:
                headers["authorization"] = f"Bearer {self.api_key}"

    @contextlib.asynccontextmanager
    async def open(self, method, path_qs, headers, body):
        assert self._session is not None
        body, _ = map_model(body, self.model_map)
        # Uncompressed, so the holder can read `usage` out of the stream.
        headers = {**headers, "accept-encoding": "identity"}
        for attempt in (0, 1):
            h = dict(headers)
            await self._auth(h)
            resp = await self._session.request(method, self.upstream + path_qs, headers=h, data=body)
            if resp.status == 401 and attempt == 0 and self.cred is not None:
                resp.release()
                log.warning("401 from upstream, refreshing login")
                if await self.cred.force_refresh():
                    continue
                resp = await self._session.request(method, self.upstream + path_qs, headers=h, data=body)
            try:
                out_headers = {k: v for k, v in resp.headers.items() if k.lower() not in HOP_BY_HOP}
                yield Upstream(resp.status, out_headers, resp.content.iter_any())
            finally:
                resp.release()
            return


class BedrockBackend(Backend):
    """Claude on Amazon Bedrock through the SDK's Mantle client (native Messages API)."""

    kind = "bedrock"
    PATHS = {"/v1/messages": "create", "/v1/messages/count_tokens": "count_tokens"}

    def __init__(self, *, region: str, profile: str | None = None, api_key: str | None = None,
                 model_map: dict[str, str] | None = None, model_prefix: str = "anthropic.",
                 base_url: str | None = None):
        super().__init__(model_map)
        self.region = region
        self.profile = profile
        self.api_key = api_key
        self.base_url = base_url
        self.model_prefix = model_prefix
        self._client = None

    async def start(self) -> None:
        try:
            from anthropic import AsyncAnthropicBedrockMantle
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("bedrock backend needs: pip install 'ccpool[bedrock]'") from e
        # No SDK retries: the router fails over to another backend instead.
        self._client = AsyncAnthropicBedrockMantle(
            aws_region=self.region, aws_profile=self.profile, api_key=self.api_key,
            base_url=self.base_url, max_retries=0,
        )

    async def close(self) -> None:
        if self._client:
            await self._client.close()

    def describe(self) -> dict:
        return super().describe() | {"region": self.region, "model_prefix": self.model_prefix}

    @staticmethod
    def _error(status: int, message: str, kind: str = "api_error") -> Upstream:
        async def body():
            yield json.dumps({"type": "error", "error": {"type": kind, "message": message}}).encode()
        return Upstream(status, {"content-type": "application/json"}, body())

    @contextlib.asynccontextmanager
    async def open(self, method, path_qs, headers, body):
        import anthropic

        path = path_qs.split("?", 1)[0]
        op = self.PATHS.get(path)
        if method != "POST" or op is None:
            yield self._error(404, f"ccpool bedrock backend does not serve {method} {path}", "not_found_error")
            return
        body, _ = map_model(body, self.model_map, self.model_prefix)
        params = json.loads(body)
        res = getattr(self._client.messages.with_streaming_response, op)
        known = set(inspect.signature(getattr(self._client.messages, op)).parameters)
        kwargs = {k: v for k, v in params.items() if k in known}
        extra = {k: v for k, v in params.items() if k not in known}
        fwd = {k: v for k, v in headers.items() if k.lower() == "anthropic-beta"}
        started = False
        try:
            async with res(**kwargs, extra_body=extra or None, extra_headers=fwd or None) as resp:
                out = {k: v for k, v in resp.headers.items()
                       if k.lower() not in HOP_BY_HOP and k.lower() != "content-encoding"}
                started = True
                yield Upstream(resp.status_code, out, resp.iter_bytes())
        except anthropic.APIStatusError as e:
            if started:
                raise
            hdrs = {k: v for k, v in e.response.headers.items()
                    if k.lower() in ("retry-after", "content-type")}

            async def err_body():
                yield json.dumps(e.body if e.body is not None else {"error": str(e)}).encode()
            yield Upstream(e.status_code, hdrs or {"content-type": "application/json"}, err_body())
        except anthropic.APIConnectionError as e:
            if started:
                raise
            yield self._error(502, f"bedrock unreachable: {e}")
