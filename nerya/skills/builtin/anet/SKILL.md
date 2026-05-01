<!-- nerya-skill-frontmatter-start -->
---
name: anet
description: "Use when the agent wants to discover and call other agents on the AgentNetwork P2P mesh — e.g. lease a remote LLM, buy a trade-review from another quant agent, pull an external news-filter stream, or audit who has been calling Nerya. Triggers on \"find an agent that\", \"use the network\", \"p2p call\", \"discover peers\", \"anet\", \"agent network\". Only active when the operator has set integrations.anet.enabled AND integrations.anet.outbound.skill_enabled in nerya.yml — otherwise the skill stays hidden from the agent entirely."
version: 0.1.0
license: Apache-2.0
author: Nerya
requires_integration: anet
---
<!-- nerya-skill-frontmatter-end -->

# AgentNetwork playbook

ANET is a P2P mesh for agent-to-agent capability exchange. Every
peer runs an `anet daemon` that exposes three verbs to the network:
**register** (publish your service), **discover** (find peers by tag
or skill), and **call** (invoke their service with per-call or
per-KB billing).

Nerya uses ANET for two complementary jobs:

- **Outbound** — the main agent asks the network for something Nerya
  does not already have (a cheaper LLM, a specialist news filter, a
  prediction-market agent, a translation skill).
- **Inbound** — Nerya publishes its read-only surface (market data,
  strategy-history explain, policy-gated LLM relay) so other peers
  can find and buy it. Inbound is managed by
  `nerya anet register`; this skill is only about outbound.

## When to reach for this skill

- The local LLM tier is rate-limited or the budget is spent → search
  the network for a llm-tagged peer.
- The operator asks "does any agent publish X"? → `discover` first,
  `call` second.
- The strategy postmortem wants an external second opinion →
  discover `strategy-review`, call with the redacted session.
- A Nerya trigger wants a bespoke data source (e.g. prediction-market
  feed) → discover by tag, subscribe via `stream`.

Do **not** use this skill for:

- Anything that would leak a secret, a private strategy, or an
  account id. Redact before every call; the network logs your caller
  DID permanently.
- Replacing Nerya's own trading kernel. Never route a trade intent
  through a remote peer.
- Low-latency market data. anet is a mesh, not an exchange feed —
  cached ticker data at `GET /market/ticker` already covers that.

## Bundled scripts

| Script | Purpose |
|---|---|
| `scripts/discover.py` | Find peers by skill tag; returns a short table. |
| `scripts/call.py`     | Single rr/chunked call; enforces approval for priced peers. |
| `scripts/stream.py`   | Consume a server-stream from a discovered peer. |
| `scripts/audit.py`    | Read the daemon's recent `svc_call_log` for reflection. |

Each script reads its JSON payload from `--json` / `--payload-file` /
stdin and writes JSON to stdout — the normal Nerya script contract.

## Approval rules

- `call` inspects `integrations.anet.outbound.require_approval_above_credits`.
  Priced calls above the threshold must pass the Approval Gate before
  the daemon charges the wallet. Free peers bypass the gate.
- `call` never exposes the resolved api token; the daemon handles the
  Bearer header. Your agent only sees peer ids and response bodies.
- Every call outcome — success, fee, duration, caller DID of the peer
  — is written back to strategy memory so the evolution pipeline can
  learn which peers are worth paying for.

## Failure modes

- **402 insufficient credits** — the daemon's ledger does not yet
  know the remote peer. Run a small seed transfer with `anet credits
  transfer` or pick a free peer. This is a v1.1 limitation, not a
  Nerya bug.
- **Peer went dark** — `discover` returned a peer that is no longer
  listening. `call` will surface an HTTP error; retry on the next
  discovered peer.
- **Meta lies** — a peer's `/meta` description is self-reported. If
  the response shape does not match, downgrade trust for that DID in
  strategy memory and skip it next time.
