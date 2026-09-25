---
type: Operations Guide
title: Deployment shapes and configuration
description: The four ways to run claude-orgtree (local-only, kiosk hosting, mailserver hub, headless), the six-level configuration hierarchy (env vars, global defaults, org settings, kiosk ceiling, org defaults, per-agent scope), and where credentials and the frozen deployment profile fit.
tags: [operations, deployment, configuration, kiosk, hub]
resource: docs/configuration.md
---

# Deployment shapes and configuration

orgtree runs standalone with nothing but a Python process, and gains
capability in three additive infrastructure tiers
([`docs/infrastructure-tiers.md`](../../docs/infrastructure-tiers.md)):
tier 1 (orgtree alone, `@org:`/`@mcp:` shortcuts), tier 2 (+ the local
mailserver hub, `@net:` and delivery receipts), tier 3 (+ a reachable address
for cross-machine mail). Nothing set up at an earlier tier stops working, and
no message sendable at one tier becomes unsendable at the next.

[`docs/setup-guide.md`](../../docs/setup-guide.md) is the task-oriented
walkthrough for four run shapes: local-only, kiosk hosting, mailserver (hub
from scratch), and headless (autonomous, API-key-billed, unattended).
Install prerequisites: Python 3.11+, Node.js 18+, and one or more provider
CLIs installed and authenticated (Claude Code, Codex, Antigravity; OpenRouter
needs only an API key). `update.ps1`/`update.sh` automate the full install
sequence including the virtualenv.

## The configuration hierarchy

[`docs/configuration.md`](../../docs/configuration.md) is the authoritative
reference. Configuration lives at six levels, each overriding or clamped by
the levels around it:

```
① process     environment variables       set by whoever launches the backend
② global      defaults.json               set once, applies to FUTURE orgs
③ org         org doc settings            per organization, editable any time
④ ceiling     kiosk / sandbox             clamps everything below it, admin-only
⑤ defaults    org-level agent defaults    what a new hire is born with
⑥ agent       NodeScope (the ⚙ panel)     per seat, clamped against ④ and the parent chain
```

Two hard rules enforced in the ledger rather than the UI: **a child can never
exceed its parent** (hire/set_scope clamp against the parent chain), and **a
kiosk ceiling clamps everything silently** — it narrows a request rather than
refusing it with an error.

### Key process-level environment variables

| Variable | Default | Purpose |
|---|---|---|
| `ORGTREE_DEPLOYMENT_PROFILE` | `standard` | `standard` or `frozen` (hardened, operator-controlled — see below) |
| `ORGTREE_DATA` | `~/orgtree` | data root: org docs, workspaces, scratch, sandboxes |
| `ORGTREE_USER_CHARTERS` | `<data root>/user/charters` | user-space charter preset directory (see [The org, credit, and provider model](../domain/org-and-providers.md)) |
| `ORGTREE_STORE` | `sqlite` | `sqlite` (canonical) or `json` (deprecated, migration on-ramp) |
| `ORGTREE_MIGRATE` | unset | set to `1` to authorize offline JSON→SQLite migration |
| `ORGTREE_PORT` / `ORGTREE_PUBLIC_PORT` / `ORGTREE_PUBLIC_ORIGIN` | `7360` / `0` (off) / — | admin API+UI (loopback-only) vs. the kiosk public gateway |
| `ORGTREE_CLAUDE`/`ORGTREE_CLAUDE_CLI`, `ORGTREE_CODEX`, `CODEX_HOME`, `ORGTREE_ANTIGRAVITY` | auto-detected | provider CLI path overrides |

Provider CLI detection and tier availability rules are in
[The org, credit, and provider model](../domain/org-and-providers.md#tiers-and-providers).

## Kiosk hosting

Any org can be exposed through a preauthenticated secret URL on a separate
public listener, with hard caps on credits, spend, and workspace storage; the
admin app itself never leaves `127.0.0.1`. `expose.ps1`/`expose-full.ps1`
open a Cloudflare quick tunnel for zero-router-setup access. Codex,
Antigravity, and OpenRouter are unavailable in kiosk orgs while sandbox
support for them is held back; sandboxed orgs additionally require a running
Docker daemon.

## The mail hub (`hub/`)

A small self-hosted Docker service (`hub/hubtool.py`, `hub/compose.yaml`)
giving every org — and every plain Claude Code session — a durable
cross-machine address. Instances dial out and long-poll; the hub holds a
queue per registered org, so nothing needs port forwarding or router
configuration. Trust model, worth reading before hosting
([`hub/README.md`](../../hub/README.md)):

- The hub sees every message in plaintext — a self-hosted trust decision, run
  on a closed network you control.
- Joining is open by design: reachability is the authorization. Addresses are
  still owned (each org self-issues a secret; the hub stores only its sha256
  fingerprint).
- The hub stores no secrets — a database leak exposes fingerprints only.
- The web UI at `/` is read-only, unauthenticated, and shows all traffic
  across every org — deliberate, for a closed collaborative network.
- TLS is not built in; a Caddy sidecar is the suggested pattern if wanted.

This is the concrete implementation of the `@net:` addressing tier described
in [Mail, audiences, and the docket](../domain/mail-and-work.md).

## The frozen deployment profile

`ORGTREE_DEPLOYMENT_PROFILE=frozen` selects a hardened, operator-controlled
profile covered in
[`docs/frozen-deployment.md`](../../docs/frozen-deployment.md): attestation
that the running install matches an approved, hash-pinned manifest
(`frozen_install.py`), a locked-down relay for sandboxed agents to reach the
host (`frozen_gateway.py`), and network policy enforcement. Any other
`ORGTREE_DEPLOYMENT_PROFILE` value raises `DeploymentConfigError`
(`deployment.py`).

## Self-updating orgs

A top-level agent (or any user-audience holder) can run
`orgtree_self_restart` to redeploy its own backend from the repo's current
commit (pulled from the remote, or a committed-but-unpushed local commit) and
rebuild the machine's mail hub, without an outside operator session. A normal
restart refuses while agents are working; `orgtree_prime_restart` schedules
one for when the machine is quiet, with an optional deadline that forces the
deploy and wakes interrupted agents if quiet never arrives. `update.ps1`
(Windows) and `update.sh` (Linux/macOS) implement the update sequence itself.

## Secrets and credentials

Credentials (provider CLI logins, OpenRouter API keys, hub org secrets, kiosk
tokens) are never documented here by value. Provider detection is read-only
— it checks CLI installation and login state but never copies credentials.
`.env`-style secret material should never be read or quoted in documentation;
if you are extending this page, describe *where* configuration lives (env
var name, settings panel, data-root file) and not its contents.

## Related pages

- [Testing and governance](testing-and-governance.md) — the decision/
  invariant registers and test-baseline discipline that govern changes to
  this configuration surface.
- [The org, credit, and provider model](../domain/org-and-providers.md) —
  what the kiosk ceiling and org-level defaults actually clamp.
