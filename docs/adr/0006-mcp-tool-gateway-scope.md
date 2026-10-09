# ADR 0006: An MCP tool gateway for request/response tool calls, default deny

## Status

Accepted (0.6.0). Date: 2026-10.

## Context

Agents increasingly call tools through the Model Context Protocol. A tool call can do more damage than a completion (close a member case, read a procedure file, move money), and agent loops repeat calls quickly. The controls a platform team applies to model spend apply equally to tools: who may call what, how fast, how much, and an audit trail. A full MCP proxy (every transport, server-initiated messages, OAuth) is a large surface.

## Decision

- `POST /mcp/{server}` proxies the request/response subset of MCP's Streamable HTTP transport: one JSON-RPC message in, the matching JSON-RPC response out, whether the upstream answers with `application/json` or `text/event-stream`. `initialize`, `ping`, `notifications/*`, `tools/list` and `tools/call` pass; other methods get `-32601`; batches get `-32600`; GET and DELETE get 405.
- **Default deny.** A team sees (in `tools/list`) and calls (via `tools/call`) only tools an entry in `config/mcp.yaml` allows, matched by glob. A team with no entry for a server gets 403 for every method on it.
- **Velocity, as in card-fraud monitoring:** sliding 60-second windows per key per tool and per team per tool, plus an identical-call rule (same tool, same arguments, same key) that stops agent loops. Daily call-count and spend caps per team per tool are read from the usage table, so they survive restarts.
- **Arguments:** PII-redacted in the audit trail by default (`redact_args`); optionally redacted before forwarding (`forward_redacted`).
- **Accounting:** allowed calls are usage rows (`provider: mcp`, `model: server/tool`) with the configured per-call cost, so they appear in showback.
- **Fail closed:** upstream timeouts, HTTP errors, malformed replies and missing upstream credentials become a `-32004` error; nothing partial is returned, and a server whose credential variable is unset is never called.
- **Auth:** clients use their gateway key (which carries the team). The gateway authenticates upstream with static headers from environment variables.

## Consequences

### Positive

- Tool calls pass the same enforcement point as model calls: least privilege per team, velocity limits against runaway agents, audit evidence and cost attribution.
- The scope is small enough to test fully against a fake MCP server over real HTTP (JSON and SSE replies).

### Negative

- Not supported: stdio servers, server-initiated streams (GET), session termination (DELETE), resumable streams, server-to-client requests (sampling, elicitation), resources and prompts, OAuth to upstream servers, per-user upstream identity.
- Velocity windows are per process, like the gateway's other rate limits.
- LLM budgets (`budgets:`) do not gate tool calls; tool spend is bounded by the per-tool `daily_usd` and counted in the same usage table, so it does count toward the team's LLM budget totals afterwards.
- Tested against a fake MCP server, not against third-party MCP servers.

## Alternatives considered

- **A full MCP proxy** (every transport, server-initiated messages, OAuth). Rejected for now: a large surface relative to the controls it adds.
- **No tool gateway** (agents call MCP servers directly). Leaves tool use outside the allow-lists, velocity limits, audit trail and showback that govern model calls.
