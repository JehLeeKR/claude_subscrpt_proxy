# ccpool: Claude Team seat pool

ccpool keeps several Claude **Team** seats logged in and spreads Claude Code
traffic across them by remaining quota and reset time.

Each seat is held by its own **holder** process, usually one container per
seat. The holder logs in **once** and keeps that login alive by refreshing it
in the background. It never logs in again per request. Claude Code talks to one
**router**. For each request the router picks a seat, and the holder for that
seat forwards the request with the seat's credential attached.

A Claude Code session keeps its whole context on the client, so switching seats
between requests loses nothing. Only the seat whose quota pays for the next
request changes, just as when you `/login` to another account mid-session.

```
 Claude Code (container 1) ─┐  ANTHROPIC_BASE_URL=http://router:8787
 Claude Code (container 2) ─┼──────────────▶ ┌──────────┐
 Claude Code (container N) ─┘  ANTHROPIC_AUTH │  router  │  picks a seat per request
                               _TOKEN=poolkey └────┬─────┘  (quota, stickiness, failover)
                                    ┌──────────────┼──────────────┐   internal key
                                    ▼              ▼              ▼
                               ┌─────────┐    ┌─────────┐    ┌─────────┐
                               │ holder  │    │ holder  │    │ holder  │  one login each,
                               │ seat-a  │    │ seat-b  │    │ seat-c  │  kept fresh
                               └────┬────┘    └────┬────┘    └────┬────┘
                                    └──────────────┼──────────────┘
                                                   ▼
                                           api.anthropic.com
```

## What each part does

**Holder** (`ccpool holder`):
- Holds one seat's login, either a `.credentials.json` from a normal Claude Code
  login or a long-lived token from `claude setup-token`.
- Refreshes the OAuth access token before it expires, including while idle.
  Rotated refresh tokens are written back to the file atomically, so the login
  survives restarts.
- Forwards Claude Code's request body as-is (context, tools, system prompt) and
  streams the reply back. It swaps only the `Authorization` header and adds the
  `oauth-2025-04-20` beta flag.
- If the API answers `401`, it refreshes once and retries.
- Reads the quota headers on every response and reports them at
  `/_ccpool/status`. The status shows no secrets.
- Accepts requests only from the router, which must present the internal key.

**Router** (`ccpool router`):
- **Scoring:** `headroom + 0.3 × expiring`.
  - `headroom` is `1 − max(5h use, 7d use)`.
  - `expiring` is unused 5h quota whose window resets soon. Spending it first
    means quota that would be lost anyway is used before quota that carries over.
  - A window whose reset time has passed counts as empty.
- **Stickiness:** a Claude Code session stays on its seat until that seat passes
  `--switch-threshold` (default 90% of either window). Sessions are identified
  by `metadata.user_id` in the request body, by a session header, or by client
  IP.
- **Failover:** on `429`, `401`, `502`, `503` or an unreachable holder, the same
  request is retried on the next seat. Nothing has reached the client at that
  point, so the client never sees the failure.
- **All seats exhausted:** it returns `429` with `retry-after` set to the moment
  the first seat frees up. Claude Code shows this as a normal rate limit.
- **Admin:** `GET /_ccpool/status`, plus
  `POST /_ccpool/holders/<name>/{drain,enable}` to take a seat out of rotation
  or put it back.

## Quick start (Docker)

### 1. Log in each seat once

On any machine with Claude Code, give each seat its own config directory:

```bash
mkdir -p deploy/seats/seat-a
CLAUDE_CONFIG_DIR=$PWD/deploy/seats/seat-a claude    # then /login as seat A's member, then exit
# repeat for seat-b, seat-c …
```

This leaves `deploy/seats/<seat>/.credentials.json`. From then on, that file
belongs to its holder. **Don't use the same login anywhere else.** The refresh
token rotates on every refresh, so two users of one login keep logging each
other out.

On macOS, Claude Code stores the login in the Keychain rather than in a file.
There, use a token instead: run `claude setup-token`, then start the holder with
`--token-file`.

### 2. Start the pool

```bash
cd deploy
cp .env.example .env        # set CCPOOL_POOL_KEY and CCPOOL_INTERNAL_KEY to random strings
docker compose up -d
```

To add or remove seats, edit the holder services and the router's `--holder`
lines in `docker-compose.yml`.

### 3. Point Claude Code at it

In each Claude Code container:

```bash
export ANTHROPIC_BASE_URL=http://<router-host>:8787
export ANTHROPIC_AUTH_TOKEN=<CCPOOL_POOL_KEY>
claude
```

### 4. Watch it

```bash
CCPOOL_POOL_KEY=... ccpool status --url http://<router-host>:8787
```

```
HOLDER        STATE        5H   RESET    7D    RESET  SCORE  SERVED
seat-a        ok          34%   2h10m   22%   4h03m   0.66     118
seat-b        ok          71%   0h25m   40%  51h12m   0.37      64
seat-c        cooling    100%   1h02m   58%  80h40m      -      90
```

## Without Docker

```bash
pip install -e .
export CCPOOL_INTERNAL_KEY=... CCPOOL_POOL_KEY=...
ccpool holder --name seat-a --oauth-file seats/seat-a/.credentials.json --port 8801 &
ccpool holder --name seat-b --token-file seats/seat-b/token --port 8802 &
ccpool router --holder seat-a=http://localhost:8801 --holder seat-b=http://localhost:8802
```

## Development

```bash
pip install -e '.[test]'
pytest
```

The tests run the router, the holders, a fake API, and a fake token endpoint in
one process. They cover:
- the credential swap and streaming
- choosing a seat and staying on it
- failover on 429 and the response when every seat is exhausted
- refreshing the OAuth token on 401 and rewriting the file
- the scoring rules

## Caveats

- **Undocumented behaviour:** the quota headers (`anthropic-ratelimit-unified-*`)
  and the OAuth refresh endpoint and client id are what Claude Code uses today.
  None of them is a documented API. You can override the endpoint and client id
  with `CCPOOL_OAUTH_TOKEN_URL` and `CCPOOL_OAUTH_CLIENT_ID`. Check both against
  real traffic before you rely on the pool.
- **Terms:** Team seats are assigned to named members. Pooling them should stay
  within what your organization's agreement with Anthropic allows. Check with
  your Team admin.
- **Router state:** the router keeps session assignments in memory. Restarting
  it only means sessions get assigned again; no context is lost, because context
  lives in Claude Code.
- **Network:** traffic between the router and the holders is plain HTTP, so keep
  the holders on a private network. Put TLS in front of the router if clients
  reach it over an untrusted network.
