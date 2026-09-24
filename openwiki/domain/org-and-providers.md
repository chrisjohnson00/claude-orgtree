---
type: Domain Concept
title: The org, credit, and provider model
description: The core product/domain model of claude-orgtree — nodes as seats in a hierarchy, credits as occupancy rather than spend, hire/retire/rehire/move/swap operations, per-provider tiers (Claude, Codex, Antigravity, OpenRouter), charters, and how to add a new provider.
tags: [domain, ledger, credits, providers, tiers, charters]
resource: backend/orgtree/ledger.py
---

# The org, credit, and provider model

This is the product-level model behind the ledger described in
[Backend architecture](../architecture/backend.md). It is the "you are the
root" idea from the README made precise.

## Nodes are seats, not just sessions

Every agent in an org is a node with a place in a hierarchy: the user is the
root ("the eye"); each node may hire its own reports. A node holds:

- an identity and provider session (tier, charter, folders/tools/visibility
  capabilities),
- a **seat cost** (credits it occupies just by existing) and a **grant**
  (credits it can allocate to its own reports),
- a lifecycle state: `live`, `archived` (retired, preserved for rehire), or
  `unrecoverable` (a broken session that still holds its seat — deliberate,
  so a crash cannot silently free budget it was already using).

## Credits are occupancy, not spend

`free(node) = grant(node) − Σ children's holdings`, where a child's holding
is `seat_cost + grant`. Credits do not buy tokens or bypass provider usage
limits — real usage and estimated dollar costs are tracked separately (see
[Backend architecture](../architecture/backend.md)'s notes on `limits.py` and
the per-provider limit modules), with spend caps layered on top for kiosk
orgs. "Give my CEO 50" allocates 50 credits of grant to spend on reports; the
CEO's own seat is not charged against that number.

## Structural operations

Hire, retire, rehire, move, swap, reallocate, and switch-model are the core
tree operations, all exposed to agents through the `orgtree_*` MCP tools
(catalog: [`docs/agent-tools.md`](../../docs/agent-tools.md)) and to the user
through the canvas (see [Frontend architecture](../architecture/frontend.md)):

- **Hire** requires explicit folders, tools, and visibility (no defaults) —
  or inserts a superior above an existing seat, which instead inherits the
  target's own scope.
- **Retire is not leaf-only**: retiring a manager auto-dissolves its subtree.
  Only self-retire with live reports still refuses.
- **Rehire** restores an archived node, optionally renaming, re-scoping,
  granting audiences, and assigning work in one call. A recoverable knowledge
  bearer must stay with its original provider; `switch_model` afterward
  changes providers.
- **Cheap-compact** resets an idle agent's session in place — same seat id,
  parent, scope, charter, grant, team — archiving its prior session as a
  knowledge bearer, avoiding a costly cold compaction. Recent work
  (`2ba1d6f feat: add org-wide and subtree bulk cheap-compact`) extends this
  from a single node to bulk operation across a whole org or subtree.
- **Move/swap** re-parent or exchange seats; structural caps (`max_depth`/
  `max_children`, 1024 by default) bind both hire and move.

Capabilities (folders rw/ro, terminal, web, file editing, subagents, MCP
servers, org-structure visibility) flow strictly downward: a parent cannot
grant what it does not hold.

## Charters

A charter is a standing role-prompt card given to a hire — a named preset
from `docs/charters/` (coordinator, curator, implementer, red team, business,
a-list-team-coordinator, review-workflow) or a repository-relative
user-space directory (`ORGTREE_USER_CHARTERS`, default
`<data root>/user/charters` — see
[Deployment and configuration](../operations/deployment-and-config.md)), or a
freehand charter typed at hire time. A user preset with the same filename as
a repo preset overrides it and is labelled "(User defined)" in the UI. The
**coordinator pattern** is the intended everyday shape: one opus coordinator
directly under the user, every worker flat beneath it, with the coordinator
decomposing asks and delegating a user audience to each hire so the
switchboard fills with direct lines. Recent work
(`9d78e8d feat: add user-space charter presets with filename-based override`)
is the override mechanism itself.

## Tiers and providers

A tier name is global within an org and already identifies its provider — no
separate provider argument is passed to hire or switch-model:

| Provider | Tiers (seat cost) | Available when |
|---|---|---|
| Claude Code | haiku (1), sonnet (2), opus (5), fable (10) | the Claude CLI can run turns |
| Codex | luna (0.2), terra (2), sol (5), astra (10, conditional); legacy gpt-reserve (0.2) | Codex CLI installed and signed in; astra offered only when the signed-in account's model inventory includes it |
| Antigravity | flash (1), pro (2) | Antigravity CLI installed and signed in |
| OpenRouter | one `or-<model>` tier per favorited model, shown by the model's own name; seat = its $/M input price (whole at/above $1/M, fractional below, never under 0.1) | an API key is set in App settings → Providers and openrouter.ai accepts it |

OpenRouter has no CLI of its own: its turns run through the Claude Code CLI
pointed at OpenRouter's Anthropic-compatible endpoint
(`ANTHROPIC_BASE_URL`/`ANTHROPIC_AUTH_TOKEN` injected per spawn, never
reaching any other provider's child). `providers.py` is a read-only registry
layered over the ledger's own `TIERS`/`MODELS` tables (the budget-bearing
source of truth), so price/tier data is never duplicated between the two.
Provider detection is read-only — it checks CLI installation and login state
but never copies or alters credentials (`accounts.py` stores identity/routing
state only; `tokens.py` is the separate credential store).

To add a new provider, follow
[`docs/adding-a-provider.md`](../../docs/adding-a-provider.md), which is the
implementation playbook cross-referencing the ledger tier tables, a new
`<provider>run.py` turn runner, and the corresponding limit-tracking module.

## Related pages

- [Backend architecture](../architecture/backend.md) — where the ledger,
  supervisor, and provider runners live in the codebase.
- [Mail and the docket](mail-and-work.md) — how hired agents communicate and
  how their work is tracked once hired.
- [Deployment and configuration](../operations/deployment-and-config.md) —
  where charter directories, provider CLI paths, and kiosk ceilings are
  configured.
