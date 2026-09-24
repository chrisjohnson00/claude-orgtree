---
type: Architecture Overview
title: Backend architecture — the orgtree FastAPI process
description: How the backend/orgtree Python package is layered — the ledger domain model, the multi-provider turn supervisor, the MCP tool surface, storage, events, and reliability primitives — with pointers into docs/ARCHITECTURE.md for implementation traps.
tags: [backend, architecture, fastapi, python]
resource: backend/orgtree/
---

# Backend architecture — the orgtree FastAPI process

`backend/orgtree/` is a single FastAPI process (~55 modules) that is the
domain source of truth for the whole product. It serves the admin UI/API, the
kiosk public gateway, and the MCP tool surface every hired agent process
loads. This page is a map; the authoritative detail lives in
[`docs/ARCHITECTURE.md`](../../docs/ARCHITECTURE.md) (implementation traps —
read it before changing ledger, supervisor, the gateways, or the canvas) and
[`docs/agent-tools.md`](../../docs/agent-tools.md) (the full tool catalog).

## Layering

```
api.py            FastAPI HTTP/WebSocket layer — UI backend, admin surface,
                   agent MCP call sink, kiosk public gateway routes
mcptool.py         MCP stdio server every agent process loads; forwards
                   orgtree_* tool calls into api.py as the real actor
supervisor.py      Turn orchestration — turns ledger rows into real CLI
                   sessions across providers (largest module, ~1.5MB)
ledger.py          Org/credit domain model (~700KB) — the aggregate root
store.py           Persistence: one JSON or SQLite file per org
events.py /        Typed, schema-generated event log with a public/private
events_table.py /  projection split
events_render.py
providers.py /     Provider axis: Claude/Codex/Antigravity/OpenRouter tier
openrouter.py /    catalogs and per-provider turn runners
codexrun.py /
antigravityrun.py
```

## The ledger — the domain heart (`ledger.py`)

`Org` is the aggregate root; `NodeDoc` is one agent ("seat") in the hierarchy.
The [org, credit, and provider model](../domain/org-and-providers.md) page
covers this in product terms — nodes, credits as occupancy, tiers, hire/
retire/rehire/move/swap. From an implementation standpoint:

- Node states are `live | archived | unrecoverable`. **"Live-for-budget" is
  not "live-for-delivery"**: `children(parent, live_only=True)` excludes only
  `archived`, so an `unrecoverable` node still holds its seat but must be
  filtered separately by any non-budget consumer (recipient lists, drive
  lists, notification fan-outs) — `docs/ARCHITECTURE.md` calls out a real
  defect this produced.
- `free = grant − Σ children's holdings`, where a child's holding is
  `seat_cost + grant`. A node's own seat is not charged against its own
  grant.
- Ledger ops **mutate `Org` in memory**; nothing persists until
  `store.save_org`, and `load_org` always re-reads from disk (no instance
  cache). Kiosk spend/credit caps rely on this ordering.
- Idempotent no-ops (retire-of-archived, rehire-of-live, switch-to-same-tier,
  duplicate requests) return SUCCESS with a warning, never raise — the
  README's "asking for what's already true is a no-op, not an error" motto
  implemented literally.
- Actor sentinels `USER`/`SYSTEM`/`EXTERN` classify org *role*, not
  authentication — a node can literally be named "user".

## The turn supervisor (`supervisor.py`)

Turns a ledger row into a real CLI session. Key ideas:

- **Resume-on-demand**: no idle processes by default; one turn is one CLI
  invocation, resumed via the provider's own resume mechanism. Eligible
  Claude-harness and Codex agents additionally keep a **warm pool**
  (`warmpool.py`) — one long-lived process per eligible agent, invalidated by
  a hash of rendered identity/argv/credentials/env rather than an enumerated
  event list, to avoid repaying MCP handshake and system-prompt cost between
  turns.
- **Cross-provider runners**: `codexrun.py` (JSON-RPC app-server client),
  `antigravityrun.py` (NDJSON stream-json per-turn process), `openrouter.py`
  (REST, actually dispatched through the Claude Code CLI pointed at
  OpenRouter's Anthropic-compatible endpoint — see
  [the org and provider model](../domain/org-and-providers.md)).
- **Compaction**: a node approaching its context limit is "split" — a
  compacted successor carries on under the same name while the
  pre-compaction self is archived in place as a consultable *knowledge
  bearer*. `handoff.py` builds verifiable citation records across that
  boundary. Recent work (`2ba1d6f feat: add org-wide and subtree bulk
  cheap-compact`, and in-flight changes to `ledger.py`/`supervisor.py`/
  `api.py` at the time of writing) extends this to bulk cheap-compact across
  an org or a subtree — see
  [Testing and governance](../operations/testing-and-governance.md) for how
  to verify changes here against the test baseline.
- **Usage/limit tracking** (`limits.py`, `codex_limits.py`,
  `antigravity_limits.py`, `openrouter_limits.py`): freeze detection and
  reset-time parsing are heavily hardened to prefer stated facts over
  inferred/cached values, because a wrong parse silently becomes a real
  dollar cost or an over-long freeze. A recent commit (`0d55fdf fix: time a
  Claude rate-limit freeze from an exhausted usage lane, back off the blind
  probe`) is representative of the care this area requires.
- **Cache continuity** (`cachecontinuity.py`, backlogged for deep-dive — see
  [quickstart backlog](../quickstart.md#backlog)) classifies whether resuming
  a session is likely to hit provider prompt cache; it deliberately does no
  I/O itself, keeping cache economics reasoning pure and testable.

## The MCP tool surface (`mcptool.py`)

Every hired agent process loads an MCP stdio server exposing the `orgtree_*`
tool catalog (organization ops, communication/audiences, reading/presenting
work, and operations like watchdogs and restarts — full list in
[`docs/agent-tools.md`](../../docs/agent-tools.md)). The tools are thin: they
forward into `api.py`, which is the real actor, and the **ledger enforces
authority, credit, folder, and provider rules** — a tool can be present in an
agent's catalog but still refuse an action outside the caller's scope. Recent
performance work trims tool descriptions and mechanics text from the agent's
system prompt (`574df27`, `b6432ee`, `c73e1c3`) while pinning the exact tool
count in tests (`a4c1739`), since the tool catalog itself is part of every
agent's loaded context budget.

## Mail and the docket

Mail (three distinct stores: `mail`, `mail_log`, `notices`) and the durable
work-item docket are both implemented in the backend but are product-level
concepts documented on their own page:
[Mail and the docket](../domain/mail-and-work.md). `workitems.py` implements
the docket; `net.py` and `refs.py` implement cross-instance addressing.

## Storage (`store.py`, `schema.py`)

One JSON file or SQLite database per org under the configured data root
(`ORGTREE_DATA`, default `~/orgtree`; see
[Deployment and configuration](../operations/deployment-and-config.md)).
`ORGTREE_STORE` selects `sqlite` (canonical default) or `json` (deprecated,
retained as a migration on-ramp and rollback route);
[`docs/sqlite-cutover.md`](../../docs/sqlite-cutover.md) is the operator
runbook. Heavy append-only logs (mail log, event log, turn error log) load
lazily and diff-save, so `save_org` only writes what changed. A global doc
lock serializes mutation; nothing under that lock may touch the network,
because the lock would otherwise stall the whole process.

## Events (`events.py`, `events_table.py`, `events_render.py`)

A declarative, single-source-of-truth event schema generates strict
validators plus a public/private projection split — a malformed event is
refused at mint time rather than written and discovered later. This backs the
turn transcript and the UI's live per-message/per-tool feed.

## Reliability and security primitives

- `opreceipts.py` — idempotency receipts for mutating agent calls, written in
  the same transaction as the mutation, with a bounded-retention watermark
  ([`docs/op-receipts.md`](../../docs/op-receipts.md)).
- `failfix.py`/`failclass.py` — allowlisted, redacted failure fixtures for
  regression tests ([`docs/failure-fixtures.md`](../../docs/failure-fixtures.md)).
- `deployment.py` — machine-wide policy (`standard` vs `frozen`), consumed
  everywhere rather than re-parsed; see
  [Deployment and configuration](../operations/deployment-and-config.md).
- `sandbox.py` — Docker-per-org isolation (backlogged for a deep-dive; see
  [quickstart backlog](../quickstart.md#backlog)).
- `bridgeauth.py` / `frozen_gateway.py` / `frozen_install.py` — rotatable
  bridge credentials, a locked-down relay, and attestation for the frozen
  deployment profile (also backlogged).
- `gitworkspace.py` / `gitapi.py` / `gitrunner.py` — the in-app Git repository
  workspace ([`docs/git-workspace.md`](../../docs/git-workspace.md)), which
  the docket links branches and commits to; see
  [Mail and the docket](../domain/mail-and-work.md).

## Where to start, and what to test

- Start at `docs/ARCHITECTURE.md`'s "Ledger & credits" and "Mail & delivery"
  sections before touching `ledger.py` or `supervisor.py`.
- `backend/tests/` has ~180 files; several are literal drift detectors that
  grep `supervisor.py` by line number or source text (`test_harvest.py`,
  `test_headless.py`, and the "turn-lifecycle" suites) — see
  [Testing and governance](../operations/testing-and-governance.md) before
  quoting a pass/fail count.
- Changes to tiers, tool descriptions, or the system prompt should be
  cross-checked against the pinned counts in `test_mcptool.py` and the
  load-bearing prompt tests (`test_phase_b_identity_prompt_load_bearing.py`,
  `test_a2_tool_description_load_bearing.py`).