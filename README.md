# claude_subscrpt_proxy

A central holder for several Claude subscription logins. It spreads Claude Code
usage across those accounts by remaining quota and reset time.

> **Status:** design draft. No code yet.

## Why this works

A Claude Code session keeps all of its context (conversation, tool state,
working directory) on the client side. The account only decides **whose quota
pays for the next request**. If you switch credentials between requests, the
session carries on and only the billing target changes. You can already see this
by running `/login` with a different account in the middle of a session.

This project automates that switch and decides when to make it.

## ⚠️ Terms-of-service risk (read first)

Anthropic's consumer subscriptions (Pro/Max) are licensed per individual.
Pooling several subscriptions to get around per-account usage limits, or
sharing logins between people or machines, may break the Consumer Terms and
Usage Policy. It can get the accounts suspended. Anthropic has also acted
against tools that send subscription OAuth tokens through non-first-party
paths. Before using this with consumer accounts, check the current terms.

The same architecture is clearly allowed with:

- **API keys** (Console / Bedrock / Vertex): load-balancing across your own
  org's keys and workspaces is normal practice.
- **Team / Enterprise** seats, where your organization administers the usage.

The design below treats the credential type as pluggable (`oauth` | `api_key`),
so the tool works in the clearly allowed mode as well.

## Architecture

```
 ┌───────────────┐   ANTHROPIC_BASE_URL=http://pool:8787
 │ Claude Code   │ ───────────────────────────────┐
 │ (container A) │                                │
 └───────────────┘                                ▼
 ┌───────────────┐                     ┌─────────────────────┐      ┌──────────────────┐
 │ Claude Code   │ ──────────────────▶ │  pool proxy         │ ───▶ │ api.anthropic.com│
 │ (container B) │                     │  - credential store │ ◀─── │                  │
 └───────────────┘                     │  - quota tracker    │      └──────────────────┘
                                       │  - scheduler        │
                                       │  - token refresher  │
                                       └─────────────────────┘
```

### Integration modes

| Mode | How it works | Switches mid-session | Notes |
|---|---|---|---|
| **A. Reverse proxy** (main) | Clients set `ANTHROPIC_BASE_URL` to the pool. For each request, the pool swaps the `Authorization` header for the chosen account's token and forwards the request with SSE streaming. | Yes, per request | Reads quota headers from every response, so it needs no polling. One pool serves many containers. |
| **B. Credential-file sync** | A sidecar rewrites `~/.claude/.credentials.json` (Linux) inside each container when the target account changes. | When Claude Code next reads the file | Doesn't touch traffic. Depends on Claude Code's re-read behaviour, which needs testing. |
| **C. Env token** | Each launch gets `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`) or `ANTHROPIC_API_KEY`. | No, set at launch | Simplest. Fits short-lived or batch containers. |

### Quota tracking

Subscription responses carry unified rate-limit headers. These were observed
from Claude Code traffic, are not a documented API, and need checking against
live traffic:

- `anthropic-ratelimit-unified-status` (`allowed` / `allowed_warning` / `rejected`)
- `anthropic-ratelimit-unified-5h-utilization`, `…-5h-reset`
- `anthropic-ratelimit-unified-7d-utilization`, `…-7d-reset`
- `anthropic-ratelimit-unified-representative-claim`

API keys get the documented `anthropic-ratelimit-*` headers (requests, input
tokens, output tokens) instead. A `429` marks the account as cooling down until
its reset time.

### Scheduling policy

For each request, score every healthy account and pick the best one:

```
headroom   = 1 - max(util_5h, util_7d * w7d)
urgency    = how soon unused quota expires   (use quota that resets soon first)
score      = headroom + α·urgency − β·recent_switch_penalty
```

- **Session stickiness:** keep one Claude Code session on one account (keyed on
  `x-claude-code-session-id` or the client's IP/container) until the account
  passes a utilization threshold. Switching often adds nothing and is easy to
  spot.
- **Failover:** on a `429` or a `rejected` status, retry the same request once
  on the next-best account.
- **Strategies:** `least-utilized` (default), `drain-expiring-first`, `round-robin`.

### Credential store

- Supported backends: in-memory, an encrypted file (age or libsodium, key from
  env), or a mounted Docker secret.
- Each account record holds its id, label, type (`oauth` | `api_key`), access
  token, refresh token, `expiresAt`, and its last quota snapshot.
- A background job refreshes OAuth access tokens before they expire.
- Tokens never appear in logs. The admin API shows them redacted.

### Admin API / CLI

- `pool add --label work-max --from ~/.claude/.credentials.json`
- `pool list` lists accounts with 5h and 7d utilization and reset times.
- `pool drain <id>` / `pool enable <id>`
- `GET /metrics` exposes Prometheus metrics.

## Open decisions

1. Language: Go (single static binary, good streaming proxy) or Python
   (asyncio + httpx, faster to iterate)?
2. Credential types for v1: OAuth subscriptions, API keys, or both?
3. Deployment: one central pool container on a Docker network, or one sidecar
   per workspace?
4. Pool access control: a shared bearer token, mTLS, or local network only?
