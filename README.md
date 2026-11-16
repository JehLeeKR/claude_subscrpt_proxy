# ccpool: Claude Team seat pool for Claude Code

ccpool keeps several Claude **Team** seats logged in and spreads Claude Code
traffic across them by remaining quota and reset time. When every seat is out
of quota, it falls back to **paid** backends: an Anthropic API key, Amazon
Bedrock, or a LiteLLM proxy. Claude Code can watch and control the pool through
**MCP**.

A Claude Code session keeps its whole context on the client. Moving between
backends mid-session only changes who pays for the next request; nothing is lost.

```
 Claude Code ─┐  ANTHROPIC_BASE_URL=http://router:8787
 Claude Code ─┼──────────────────▶ ┌──────────────────────────┐ ◀── MCP (/mcp): status,
 Claude Code ─┘                    │ router                   │     usage, policy, pin, drain
                                   │ 1. seats, by quota       │
                                   │ 2. paid, if seats empty  │
                                   │    and within budget     │
                                   └────────────┬─────────────┘
           ┌───────────┬───────────┬────────────┼───────────┬────────────┐
           ▼           ▼           ▼            ▼           ▼            ▼
       ┌───────┐   ┌───────┐   ┌───────┐   ┌────────┐  ┌─────────┐  ┌─────────┐
       │seat-a │   │seat-b │   │seat-c │   │  api   │  │ bedrock │  │ litellm │  holders: one
       │ OAuth │   │ OAuth │   │ OAuth │   │API key │  │  AWS    │  │ v-key   │  credential each,
       └───┬───┘   └───┬───┘   └───┬───┘   └───┬────┘  └────┬────┘  └────┬────┘  kept alive
           └─────── api.anthropic.com ─────────┘      Bedrock Mantle   your LiteLLM
```

## Components

**Holder** (`ccpool holder --kind …`): one process (or container) per backend.

| kind | credential | upstream |
|---|---|---|
| `subscription` | Team seat login: `.credentials.json`, refreshed in the background (rotated refresh tokens are saved), or a `claude setup-token` token | api.anthropic.com |
| `api_key` | Console API key | api.anthropic.com |
| `bedrock` | standard AWS credential chain or a Bedrock API key | Bedrock Mantle (native Messages API), via the Anthropic SDK |
| `litellm` | LiteLLM virtual key | your LiteLLM proxy's `/v1/messages` |

- **Forwarding:** passes Claude Code's request body through unchanged.
  `--model-map` can rename models, and Bedrock gets the `anthropic.` prefix by
  default. Replies stream back as-is.
- **Quota:** reads the seat quota headers (5h and 7d use and reset times) and
  records `429`s.
- **Usage:** records per-model token usage (input, output, cache reads and
  writes) and prices it. Paid rows are real spend. Seat rows are priced at API
  list price to show what the seats are worth.

**Router** (`ccpool router`):
- **Seats first:** a seat is scored on `headroom + 0.3 × expiring`. Headroom is
  `1 − max(5h use, 7d use)`. Expiring is unused 5h quota that resets soon. Each
  session stays on its seat until it passes `switch_threshold`.
- **Paid fallback, only when every seat is exhausted:** backends are tried in
  `priority` order, and only while `paid_fallback` is on and today's paid spend
  is under `paid_daily_budget_usd`. A session moves back to a seat as soon as
  one has quota again.
- **Failover inside a request:** on `401`, `403`, `429`, `5xx` or an unreachable
  holder, the request goes to the next backend before any bytes reach the client.
- **Sessions:** a Claude Code session is recognised by the session UUID in
  `metadata.user_id`, so the MCP tools and the status line can say where a given
  session is routed.

## Control from Claude Code

### MCP

The router serves MCP at `/mcp` (streamable HTTP), so client containers need
nothing installed:

```bash
claude mcp add --transport http ccpool http://router:8787/mcp \
  --header "Authorization: Bearer $CCPOOL_ADMIN_KEY"
```

Or commit [`deploy/client/mcp.json`](deploy/client/mcp.json) as the project's
`.mcp.json`. For stdio clients, `ccpool mcp --url http://router:8787` bridges to
the same endpoint.

| Tool | Does |
|---|---|
| `pool_status` | Seats and paid backends: state, 5h and 7d use and reset times, today's spend, policy, and which backend each session is on |
| `usage_report` | Tokens and cost per backend (`today` or `all`) |
| `session_backend` | The backend one session is routed to |
| `set_policy` | Change `switch_threshold`, `paid_fallback` and `paid_daily_budget_usd` |
| `set_backend_enabled` | Take a backend out of rotation or put it back |
| `pin_session` | Force a session onto one backend, or remove the pin |

You can then ask Claude Code things like *"how much seat quota is left?"*,
*"cap Bedrock spend at $20 today"* or *"move this session to seat-b"*.

### Status line

Set this in the client's `settings.json` (see
[`deploy/client/settings.json`](deploy/client/settings.json)):

```json
"statusLine": { "type": "command", "command": "ccpool statusline --url http://router:8787" }
```

It shows the current session's backend, for example
`ccpool: seat-a 5h 42% · 7d 10%` or `ccpool: bedrock (paid fallback)`.

### CLI

`ccpool status`, `ccpool usage [--period all]`, `ccpool drain NAME`,
`ccpool enable NAME`.

## Quick start (Docker)

### 1. Log in each seat once

```bash
mkdir -p deploy/seats/seat-a
CLAUDE_CONFIG_DIR=$PWD/deploy/seats/seat-a claude   # /login as that seat's member, then exit
# repeat per seat
```

This leaves `deploy/seats/<seat>/.credentials.json`. From now on that login
belongs to its holder; **don't use it anywhere else**. The refresh token rotates
on every refresh, so two users of one login keep logging each other out. On
macOS the login is kept in the Keychain rather than a file, so use
`claude setup-token` and start the holder with `--token-file` instead.

### 2. Configure and start

```bash
cd deploy
cp .env.example .env     # pool, admin and internal keys, plus the paid backend credentials you use
$EDITOR router.toml      # seats, paid fallbacks, policy
docker compose up -d --build
```

### 3. Point Claude Code at the router

```bash
export ANTHROPIC_BASE_URL=http://<router-host>:8787
export ANTHROPIC_AUTH_TOKEN=<CCPOOL_POOL_KEY>
```

You can also put these in `settings.json` under `env`.

## Keys

| Key | Who holds it | Grants |
|---|---|---|
| `CCPOOL_POOL_KEY` | every Claude Code client | sending requests; looking up its own session's backend |
| `CCPOOL_ADMIN_KEY` | operators and the MCP config | status, usage, policy, drain, pin (defaults to the pool key if unset) |
| `CCPOOL_INTERNAL_KEY` | router and holders only | holder endpoints |

## Development

```bash
pip install -e '.[test]'
pytest
```

The tests run the router, holders, a fake Messages API and a fake OAuth token
endpoint in one process. They cover:
- credential swap and streaming
- choosing a seat, staying on it, and moving off it at the threshold
- failover on 429
- paid fallback and moving back to a seat
- the daily budget and turning paid fallback off
- the LiteLLM and Bedrock backends: model mapping and passing unknown fields through
- token accounting and pricing
- OAuth refresh on 401
- every MCP tool
- the status-line endpoint

## Caveats

- **Undocumented interfaces:** the quota headers
  (`anthropic-ratelimit-unified-*`), and the OAuth refresh endpoint and client ID,
  are what Claude Code uses today. They aren't a documented API. You can
  override the endpoint and client ID with `CCPOOL_OAUTH_TOKEN_URL` and
  `CCPOOL_OAUTH_CLIENT_ID`. Check them against real traffic first.
- **Prices:** the price table uses first-party list prices. Bedrock and providers
  behind LiteLLM may charge differently; use `--prices prices.json` on those
  holders to override. Cache writes are estimated at 1.25× input for the 5-minute
  TTL and 2× for 1 hour.
- **Budget:** the router learns paid spend by polling the holders (every 10s by
  default), so a burst can go a little over the budget.
- **State is in memory:** usage totals reset when a holder restarts. Session
  assignments reset when the router restarts, which loses no context.
- **Terms:** Team seats are assigned to named members. Pool them only within what
  your organization's agreement with Anthropic allows, and check with your Team
  admin.
- **Network:** traffic between the router and the holders is plain HTTP. Keep the
  holders on a private network, and put TLS in front of the router for
  untrusted networks.
