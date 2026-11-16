"""Credential sources for a holder.

A holder logs in once and keeps that login alive. Two sources are supported:

* ``StaticToken``: a long-lived token from ``claude setup-token``. It needs no
  refresh.
* ``OAuthFile``: a Claude Code ``.credentials.json``. The holder refreshes the
  access token before it expires and writes the rotated refresh token back to
  the file, so the login survives restarts.

The refresh token rotates on every refresh. The file a holder owns must not
also be used by a normal Claude Code install, or the two will invalidate each
other's refresh tokens.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import aiohttp

log = logging.getLogger(__name__)

# The values Claude Code uses at the time of writing. They are not a public API,
# so both can be overridden from the CLI or the environment.
DEFAULT_TOKEN_URL = os.environ.get(
    "CCPOOL_OAUTH_TOKEN_URL", "https://console.anthropic.com/v1/oauth/token"
)
DEFAULT_CLIENT_ID = os.environ.get(
    "CCPOOL_OAUTH_CLIENT_ID", "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
)

REFRESH_MARGIN_S = 10 * 60  # refresh when less than this much lifetime remains


class CredentialError(RuntimeError):
    pass


class Credential:
    """Base class. ``token()`` always returns a currently valid access token."""

    async def token(self) -> str:
        raise NotImplementedError

    async def force_refresh(self) -> bool:
        """Refresh after the upstream rejected the token. Returns True if a new token exists."""
        return False

    def expires_at(self) -> float | None:
        return None

    async def keepalive(self, stop: asyncio.Event) -> None:
        """Background loop that keeps the login fresh while the holder is idle."""
        await stop.wait()

    def describe(self) -> dict:
        return {"kind": type(self).__name__}


class StaticToken(Credential):
    def __init__(self, value: str):
        if not value:
            raise CredentialError("empty token")
        self._value = value.strip()

    async def token(self) -> str:
        return self._value


@dataclass
class _OAuthState:
    access_token: str
    refresh_token: str
    expires_at: float  # unix seconds
    raw: dict  # the whole file, so unknown fields survive a rewrite


class OAuthFile(Credential):
    def __init__(
        self,
        path: str | Path,
        *,
        token_url: str = DEFAULT_TOKEN_URL,
        client_id: str = DEFAULT_CLIENT_ID,
        session: aiohttp.ClientSession | None = None,
    ):
        self.path = Path(path)
        self.token_url = token_url
        self.client_id = client_id
        self._session = session
        self._lock = asyncio.Lock()
        self._state = self._load()

    # file handling -------------------------------------------------------

    def _load(self) -> _OAuthState:
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, ValueError) as e:
            raise CredentialError(f"cannot read {self.path}: {e}") from e
        oauth = raw.get("claudeAiOauth") or {}
        try:
            return _OAuthState(
                access_token=oauth["accessToken"],
                refresh_token=oauth["refreshToken"],
                expires_at=float(oauth.get("expiresAt", 0)) / 1000.0,
                raw=raw,
            )
        except KeyError as e:
            raise CredentialError(f"{self.path}: missing claudeAiOauth.{e.args[0]}") from e

    def _save(self) -> None:
        s = self._state
        raw = dict(s.raw)
        oauth = dict(raw.get("claudeAiOauth") or {})
        oauth.update(
            accessToken=s.access_token,
            refreshToken=s.refresh_token,
            expiresAt=int(s.expires_at * 1000),
        )
        raw["claudeAiOauth"] = oauth
        s.raw = raw
        # Atomic replace so a crash never leaves a half-written file behind.
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".cred-")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(raw, f, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    # refresh ---------------------------------------------------------------

    def expires_at(self) -> float:
        return self._state.expires_at

    def _needs_refresh(self) -> bool:
        return self._state.expires_at - time.time() < REFRESH_MARGIN_S

    async def _refresh_locked(self) -> None:
        session = self._session or aiohttp.ClientSession()
        try:
            async with session.post(
                self.token_url,
                json={
                    "grant_type": "refresh_token",
                    "refresh_token": self._state.refresh_token,
                    "client_id": self.client_id,
                },
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                body = await resp.text()
                if resp.status != 200:
                    raise CredentialError(f"refresh failed: HTTP {resp.status}: {body[:200]}")
                data = json.loads(body)
        finally:
            if self._session is None:
                await session.close()

        self._state.access_token = data["access_token"]
        # Refresh tokens rotate; keep the old one only if none was returned.
        self._state.refresh_token = data.get("refresh_token") or self._state.refresh_token
        self._state.expires_at = time.time() + float(data.get("expires_in", 3600))
        self._save()
        log.info("refreshed OAuth token for %s, valid for %ss", self.path, data.get("expires_in"))

    async def token(self) -> str:
        if self._needs_refresh():
            async with self._lock:
                if self._needs_refresh():
                    await self._refresh_locked()
        return self._state.access_token

    async def force_refresh(self) -> bool:
        stale = self._state.access_token
        async with self._lock:
            if self._state.access_token != stale:
                return True  # another request already refreshed it
            try:
                await self._refresh_locked()
                return True
            except CredentialError as e:
                log.error("%s", e)
                return False

    async def keepalive(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.token()
            except CredentialError as e:
                log.error("keepalive: %s", e)
            # Wake up a little before the refresh margin is reached.
            delay = max(30.0, self._state.expires_at - time.time() - REFRESH_MARGIN_S + 5)
            try:
                await asyncio.wait_for(stop.wait(), timeout=min(delay, 3600))
            except asyncio.TimeoutError:
                pass

    def describe(self) -> dict:
        return {
            "kind": "OAuthFile",
            "path": str(self.path),
            "expires_in_s": int(self._state.expires_at - time.time()),
            "subscription": (self._state.raw.get("claudeAiOauth") or {}).get("subscriptionType"),
        }


def load_credential(
    *, token: str | None = None, token_file: str | None = None, oauth_file: str | None = None
) -> Credential:
    if token:
        return StaticToken(token)
    if token_file:
        return StaticToken(Path(token_file).read_text())
    if oauth_file:
        return OAuthFile(oauth_file)
    env = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if env:
        return StaticToken(env)
    raise CredentialError(
        "no credential: pass --token-file, --oauth-file, or set CLAUDE_CODE_OAUTH_TOKEN"
    )
