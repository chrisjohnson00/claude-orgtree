---
type: Reference
title: OpenWiki Quickstart — claude-orgtree
description: Entry point to the claude-orgtree documentation set, covering what the project is, how it is organized, and where to find backend architecture, frontend canvas, domain model, mail/work, and operations documentation.
tags: [quickstart, overview, orgtree]
resource: README.md
---

# claude-orgtree — OpenWiki quickstart

**claude-orgtree** is a persistent, visual **organization of coding agents**: a
tree of addressable Claude Code, Codex, Antigravity, and OpenRouter-backed
sessions, each with a credit budget, running on an office-room canvas, with
full agent-to-agent delegation. The user sits at the root ("the eye") as
overseer; they hire top-level agents, those agents hire their own reports, and
each agent runs through its provider's CLI (or, for OpenRouter, an
Anthropic-compatible gateway) for its selected tier.

The project's own design motto: *one thing, done very very well* — permit as
much as possible, close gaps with minimal-friction shortcuts, step out of the
way. Read the full product tour in [`/README.md`](../README.md); this wiki is
a synthesis and map layer over the extensive existing docs in
[`/docs/`](../docs/README.md), not a replacement for them.

## How this repository is organized

- **`backend/orgtree/`** — a single FastAPI process (~55 Python modules) that
  is the domain source of truth: the org/credit ledger, the multi-provider
  turn supervisor, mail, the docket, and every reliability primitive.
- **`frontend/src/`** — a React + TypeScript + Vite canvas application that
  renders the org as a pannable/zoomable office-room graph and lets a desk
  zoom into a full chat surface.
- **`hub/`** — an optional, separately-run Docker mail-hub service that lets
  orgtree instances on different machines mail each other.
- **`docs/`** — the pre-existing, actively maintained documentation set.
  [`docs/README.md`](../docs/README.md) is its own map; treat it as primary
  source material. [`DECISIONS.md`](../DECISIONS.md) is the normative decision
  register, [`docs/INVARIANTS.md`](../docs/INVARIANTS.md) is the narrower,
  higher-authority register of explicit user-stated invariants, and
  [`docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md) records implementation
  traps a refactor could silently break. See
  [Testing and governance](operations/testing-and-governance.md) for how these
  three relate and how the test suite guards them.
- **`docs/charters/`** — standing role-prompt presets (coordinator, curator,
  implementer, red team, business, a-list-team-coordinator) offered in the
  hire form; see [The org, credit, and provider model](domain/org-and-providers.md).

## Where to go next

| Page | Covers |
|---|---|
| [Backend architecture](architecture/backend.md) | The FastAPI process: `api.py`/`mcptool.py` surface, `ledger.py`, `supervisor.py`, provider runners, storage, events, reliability primitives. |
| [Frontend architecture](architecture/frontend.md) | The React canvas app: coordinate spaces, camera, the desk's inverted-scale regime, pinning/windows, mobile, and the API/live-update layer. |
| [The org, credit, and provider model](domain/org-and-providers.md) | Nodes/seats, credits as occupancy, hire/retire/rehire/move/swap, tiers per provider, charters, and how to add a new provider. |
| [Mail and the docket](domain/mail-and-work.md) | The three mail stores, audiences, cross-instance addressing, and the durable work-item docket. |
| [Deployment and configuration](operations/deployment-and-config.md) | The six configuration levels, the four setup shapes (local/kiosk/mailserver/headless), and the mail hub. |
| [Testing and governance](operations/testing-and-governance.md) | Backend pytest and frontend browser-probe conventions, the test-baseline caveat, and the DECISIONS/INVARIANTS/ARCHITECTURE governance triad. |

## What to watch out for as a future agent

- **Read `docs/ARCHITECTURE.md` before touching ledger, supervisor, the
  gateways, or the canvas.** It documents traps — implicit contracts that
  look obvious from the source but are wrong — that a naive refactor would
  silently violate. This wiki summarizes the highlights but is not a
  substitute.
- **`docs/INVARIANTS.md` outranks `DECISIONS.md`, which outranks ordinary
  code comments.** If they conflict, the invariant wins; record the conflict,
  do not quietly narrow the invariant.
- **`main` is not green.** [`docs/test-baseline.md`](../docs/test-baseline.md)
  is the only trustworthy way to tell your breakage from pre-existing
  failures — some suites are literal source-text drift detectors keyed to
  `supervisor.py`, so re-measure at your own tip rather than trusting a
  quoted count.
- **This repository already runs OpenWiki on a schedule**
  (`.github/workflows/openwiki-update.yml`, referenced from `AGENTS.md` and
  `CLAUDE.md`). Prefer updating source/docs and letting OpenWiki regenerate
  over hand-editing generated pages under `openwiki/`.

## Backlog

- **Provider cache continuity/economics** (`backend/orgtree/cachecontinuity.py`,
  `docs/cache-continuity.md`, `docs/cache-hazards.md`, `docs/cache-economics.md`) —
  the classification of whether resuming a session is likely to hit a
  provider's prompt cache, and its cost implications. Touched on briefly in
  [Backend architecture](architecture/backend.md); deferred as a standalone
  deep-dive because it is a large, fast-moving subsystem best read directly
  from its docs when a change actually touches it.
- **Sandbox isolation and frozen-deployment attestation**
  (`backend/orgtree/sandbox.py`, `frozen_install.py`,
  `frozen_gateway.py`, `docs/frozen-deployment.md`) — kiosk/operator security
  boundary. Summarized at overview level in
  [Deployment and configuration](operations/deployment-and-config.md);
  deferred because the full attestation/manifest model needs its own
  focused pass.
- **Mail hub network protocol detail** (`hub/hubtool.py`,
  `docs/mailserver-spec.md`) — the queued → sent → delivered → read receipt
  ladder and registration/auth mechanics. Summarized at overview level in
  [Deployment and configuration](operations/deployment-and-config.md);
  deferred to its own spec doc for wire-level detail.
- **Mobile UI** (`frontend/src/mobile.tsx`, `docs/mobile-spec.md`) — the
  OS-allowlist mobile mode and full-screen "sheet" desk. Mentioned briefly in
  [Frontend architecture](architecture/frontend.md); deferred because it is a
  secondary, still-evolving surface relative to the desktop canvas.
