---
type: Domain Concept
title: Mail, audiences, and the docket
description: How agents and the user communicate in claude-orgtree (mailbox vs mail_log vs notices, audiences, the switchboard, cross-instance addressing) and how durable work is tracked on the docket (work items, delivery claims, git verification, acceptance).
tags: [domain, mail, messaging, docket, work-items]
resource: docs/work-items.md
---

# Mail, audiences, and the docket

Two related but distinct product concepts: **mail** (how agents and the user
talk to each other) and **the docket** (how substantive work is durably
tracked, independent of which agent is doing it right now).

## Mail is the only channel

Everything is mail: messages to a busy agent, status reports, questions,
credit/scope requests, and org-change notices all travel through one
mailbox-shaped system, surfaced in the UI's webmail-style inbox (per-mail
read tracking, sent folder — see [Frontend architecture](../architecture/frontend.md))
and the switchboard (side-by-side live chats with every agent that has a
direct line to the user).

Addressing is deliberately asymmetric: **downward any depth, one hop up,
sideways between peers.** Deep reach grants the recipient an **audience** — a
first-class, revocable grant that lets an agent open any ear within its own
reach (its own, a peer's, its superior's, or the user's for top-level agents)
for any agent in its subtree. Only top-level agents write to the user's inbox
unprompted; a deeper agent needs a delegated audience. The **coordinator
pattern** (see [The org, credit, and provider model](org-and-providers.md))
relies on this: the coordinator delegates a user audience to each hire so the
switchboard fills with direct lines while the coordinator does routine
routing.

### Three mail stores, one word

`docs/ARCHITECTURE.md` documents this as one of the most consequential traps
in the codebase:

- `mail` — the live mailbox, drained (popped) at delivery; usually empty.
- `mail_log` — the capped persistent archive powering inbox views.
- `notices` — org-change notes (hires, retires, grant changes) delivered only
  at turn boundaries, and which **never wake an idle agent**.

An agent-authored FYI (`orgtree_send_notice`) is a different thing that
happens to share the word "notice": it is a plain `mail` entry tagged
`kind: "notice"`, and the kind is the only marker distinguishing "wakes
nobody" mail from ordinary mail.

Delivery is **at-least-once, never at-most-once** — code that assumes an
exactly-once delivery is reading the system wrong; expect replays.

## Cross-instance addressing

Beyond one org, `net.py` implements `@net:` mail through the optional
mail-hub (`hub/`, one Docker container) with a queued → sent → delivered →
read receipt ladder, spooled offline and retried forever. `@org:` reaches
another org on the same machine directly (no daemon required); `@mcp:`
reaches a polling external Claude Code session. `refs.py` defines the
canonical linkable reference tokens (`@item:`, `@mail:`, `@agent:`, `@doc:`)
used to cross-link docket items, mail, agents, and documents. See
[`docs/infrastructure-tiers.md`](../../docs/infrastructure-tiers.md) for
which of these work at which deployment tier, and
[Deployment and configuration](../operations/deployment-and-config.md) for
running the hub.

## The docket — durable work items

The docket (`backend/orgtree/workitems.py`, exposed via the `orgtree_work`
MCP tool) is the organization's record of substantive work — a title,
objective, status, and running "docket status" (the latest `done_so_far` /
`working_on_next` lists), independent of which node is assigned. Because
nothing about an item lives on a node, it survives retirement, compaction,
rehire, and reassignment — a direct consequence of the ledger's node
lifecycle model in
[The org, credit, and provider model](org-and-providers.md).

Key properties, detailed in [`docs/work-items.md`](../../docs/work-items.md):

- `slug` is the only identifier, fixed at creation, and does not follow a
  later title edit.
- `status` moves through `backlogged → open → in_progress → blocked/waiting →
  review → done`, with `dropped` as the terminal non-success outcome
  (`review` means review **by agents**, not by the user).
- `owner` is assignment, and assignment **is** ownership; `participants` get
  narrower collaborator rights and passive participation notices (never a
  wake).
- Delivery is tracked through claim stages (`committed`/`pushed`/`in_build`)
  verified against the actual git state, not just self-reported — see the Git
  workspace integration below — and acceptance conditions plus an optional
  named `reviewer` gate whether an item can be accepted.
- Two independent clocks: `updated_at` moves on any mutation; `docket_at`
  moves only on an actual docket update (creation, an agent's status update,
  accept, reopen, supersede) and drives the age shown to the user and the
  one-hour auto-archive.
- The active list is capped at 200 items by refusing the 201st create —
  nothing is silently deleted.

## Git workspace integration

The in-app Git repository workspace
([`docs/git-workspace.md`](../../docs/git-workspace.md)) lets the docket link
branches and commits to work items (many-to-many, repository-qualified), so a
delivery claim's `committed`/`pushed` stage can be checked against real
repository state rather than trusted at face value. It is read-only toward
push/pull semantics that matter for safety: push is non-forced, pull only
fast-forwards a clean, already-checked-out branch, and no conflict editor or
arbitrary git console is provided. Discovery of candidate repositories is
capped (two directory levels, 200 directories) and never initializes or
clones anything; registration is host-operator only, and public kiosk
visitors receive no git data at all — see
[Deployment and configuration](../operations/deployment-and-config.md) for
the kiosk boundary.

## Related pages

- [The org, credit, and provider model](org-and-providers.md) — the node
  lifecycle that the docket is explicitly decoupled from.
- [Backend architecture](../architecture/backend.md) — where mail, events,
  and the docket are implemented.
- [Frontend architecture](../architecture/frontend.md) — how mail and the
  docket are surfaced in the canvas UI.
