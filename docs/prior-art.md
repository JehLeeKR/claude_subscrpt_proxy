# Prior art (surveyed 2026-11-16)

Several open-source tools already pool Claude subscriptions for Claude Code.
None of them was verified beyond its README and docs. Star counts are as shown
on GitHub on the survey date.

## Pooling proxies: same shape as ccpool

| Project | Stack | Overlap with ccpool | Missing compared with ccpool |
|---|---|---|---|
| [TeamClaude](https://github.com/KarpelesLab/teamclaude) (~500★, MIT) | Node, no dependencies, GHCR image | Proxy via `ANTHROPIC_BASE_URL`; reads `anthropic-ratelimit-unified-*` for 5h and 7d; threshold rotation (98%) preferring the soonest weekly reset; separates quota 429s from burst 429s and paces after a switch; pins conversations by `x-claude-code-session-id` plus a digest of the opening messages; hold-until-reset; OAuth refresh with write-back; API-key accounts and Anthropic-compatible third-party backends at low priority; **opt-in MCP endpoint** for quota and switching; terminal UI and web dashboard | No Bedrock or LiteLLM backend type; no daily dollar budget; one process holds every credential; scoped to "one person, own accounts" |
| [alPool](https://github.com/ahmedLawal/alPool) / maxpool (forks of TeamClaude) | Node | Ranks accounts by remaining quota ÷ time to reset; 90% threshold; session home account with return after recovery; probes quota without spending | No Bedrock or LiteLLM; no MCP; macOS-centric |
| [claude-rotate](https://github.com/doxaras/claude-rotate) (46★, MIT) | **Python** (FastAPI, httpx) | `setup-token` per account; per-device keys; consume-first rotation (soonest weekly reset first); unified quota headers; web analytics panel | One active account for **all** devices, so no per-session stickiness; no paid fallback; no MCP; no Docker yet |
| [claude-relay-service](https://github.com/Wei-Shaw/claude-relay-service) (~12.8k★, MIT) | Node + Redis | Multi-account Claude OAuth pool with failover; per-key rate and cost limits; web admin; cost statistics; Docker | No Bedrock; no MCP; **critical admin auth bypass in versions ≤1.1.248**; successor is "sub2api" |
| [better-ccflare](https://github.com/tombii/better-ccflare) (fork of [ccflare](https://github.com/snipeship/ccflare)) | Bun/TS | Load balancing across accounts, failover on rate limits, request dashboard; Bedrock accounts in progress (v3.2 betas) | No MCP; no quota-aware ranking documented |
| [CLIProxyAPI](https://mintlify.com/router-for-me/CLIProxyAPI) | Go | Claude OAuth round-robin or fill-first, with priority tiers | Plain round-robin hurts prompt cache; no budget |

## Client-side account switchers (swap the login; no proxy)

- [clauth](https://docs.rs/crate/clauth/0.7.3): Rust terminal UI. Supports Team and Enterprise OAuth, tracks 5h and 7d use, has an auto-switch fallback chain, and ships an **MCP plugin** to list or switch accounts or hand a prompt to another account.
- [cux](https://pkg.go.dev/github.com/inulute/cux@v0.3.13): adds a `/switch` slash command and switches automatically at a threshold.
- Others with the same idea: [claude-swap](https://github.com/realiti4/claude-swap), [ccm](https://dr5hn.github.io/ccm), [c2switcher](https://github.com/gmh5225/c2switcher).

## Gateways: paid tier only

- **LiteLLM** is documented by Anthropic as a Claude Code gateway
  ([docs](https://code.claude.com/docs/en/llm-gateway.md)). It handles Bedrock and
  API-key fallback chains and per-key budgets. It can pass a subscription OAuth
  token through to Anthropic
  ([tutorial](https://docs.litellm.ai/docs/tutorials/claude_code_max_subscription)),
  but it does not rotate across seats by quota. There is a
  [known issue](https://github.com/BerriAI/litellm/issues/24436) where OAuth and
  Bedrock credentials get mixed up. Versions 1.82.7 and 1.82.8 had a malware
  advisory.

## Status of the official product

Claude Code has open feature requests for switching or load-balancing accounts
([#26777](https://claudeissues.com/issue/26777-feature-auto-switch-accounts-when-rate-limited),
[#43978](https://claudeissues.com/issue/43978-feature-request-multi-account-load-balancing-to-reduce-rate-limit-interruptions)).
Nothing in Claude Code itself does this.

## What this means for ccpool

**TeamClaude covers most of the core.** It has quota-header routing, stickiness,
refresh, API-key fallback, MCP and Docker. ccpool adds three things:
1. One holder process per credential. A seat's login is never in the same process
   as other seats', and holders can live in separate containers or hosts.
2. Bedrock and LiteLLM as first-class paid tiers, with a daily dollar budget and an
   automatic move back to seats.
3. Python, and one central router shared by many Claude Code containers.

**Ideas worth borrowing** (from TeamClaude, alPool and claude-rotate):
- Tell quota-exhausted 429s (`unified-status: rejected`) apart from burst or
  per-minute 429s. Don't rotate on a burst; pace instead.
- Key pins on `x-claude-code-session-id` (TeamClaude confirms Claude Code sends
  it) plus a digest of the opening messages, so subagents are placed separately.
- **Prompt cache:** the cache doesn't carry across organizations. It mostly
  carries within one, which suits a single Team org. Optionally trigger
  "compact before switch".
- Hold-until-reset as an option, instead of returning 429 at once.
- Probe quota without spending, so idle seats show fresh numbers.
- Persist pins and usage across restarts.
- TeamClaude rewrites `account_uuid` / `metadata.user_id` to match the serving
  account. We need to check whether Anthropic validates this for Team seats.

**Alternative that needs no new code:** TeamClaude for the seats, with its
third-party "Anthropic-compatible" backend pointed at **LiteLLM**. LiteLLM then
handles Bedrock and API keys, with its own per-key budgets. The trade-offs are
Node rather than Python, one process holding every seat's credential, and no
automatic move back to seats while LiteLLM still serves.

**Terms:** every one of these projects calls multi-account pooling a gray area.
TeamClaude explicitly excludes sharing between people. Pooling Team seats that
belong to named members across many users is that kind of sharing, so get the
Team admin's or Anthropic's sign-off first.
