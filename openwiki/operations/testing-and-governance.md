---
type: Operations Guide
title: Testing, decisions, and invariants — how this repo governs itself
description: How claude-orgtree tracks normative decisions (DECISIONS.md), explicit app-state invariants (INVARIANTS.md), implementation traps (ARCHITECTURE.md), and test-baseline discipline (test-baseline.md) — required reading before changing ledger, supervisor, or canvas internals.
tags: [operations, testing, governance, decisions, invariants]
resource: docs/test-baseline.md
---

# Testing, decisions, and invariants — how this repo governs itself

This repository is unusually explicit about *why* rules exist and how to
avoid re-litigating them. Three registers plus one measurement discipline
work together; each has a narrow, non-overlapping job.

## The three registers

| File | Scope | Outranks |
|---|---|---|
| [`docs/INVARIANTS.md`](../../docs/INVARIANTS.md) | Explicit **user-stated** hard constraints on running app state — narrowest, and the final word | `DECISIONS.md` |
| [`DECISIONS.md`](../../DECISIONS.md) | The normative decision register — what must be true, and who decided it, organized by domain (not time); a superseded entry is rewritten in place with the old reading kept in a `Was.` slot | — |
| [`docs/ARCHITECTURE.md`](../../docs/ARCHITECTURE.md) | Implementation traps — rules that would evaporate under a refactor, not durable decisions | — |

The sorting rule the project uses for new material: **if a rule would
survive a refactor, it is a decision (`DECISIONS.md`); if it would evaporate
the moment the code was restructured, it is a trap
(`docs/ARCHITECTURE.md`).** An `INVARIANTS.md` entry sits above both: a
decision may implement or explain an invariant, or add a stricter guarantee,
but must never weaken, supersede, or reinterpret one — a conflict is itself
an enforcement failure, recorded as a `known_gap`, not resolved by quietly
narrowing the invariant.

`docs/PLAN.md`/`docs/history/PLAN.md` and `docs/attic/` are explicitly
**historical, not current** — read them for context, verify any claim in
source before treating it as current behavior.

## Test-baseline discipline

`main` is not green. [`docs/test-baseline.md`](../../docs/test-baseline.md)
exists because "expect a fully green run" is the wrong bar for this
repository — the right one is parity against a baseline **you measured
yourself, at your own tip**. Before attributing a test failure to your own
change:

1. Re-run the baseline at your current commit — it costs a few minutes and
   is the actual point of the document.
2. Do not quote a failure count from the document's prose; trust the table
   and its per-row "last confirmed" tip instead — counts in prose have
   already gone stale and caused confusion once.
3. Some suites (`harvest`, `headless`, and the `turn-lifecycle` suites) are
   not conventional tests — they are **drift detectors** that grep
   `supervisor.py` by line number or literal source text. Any change that
   shifts that file changes what they report, in either direction: a landing
   can quietly repair one or break a previously-fine one. A `turn-lifecycle`
   number is meaningless without stating which of its (multiple) invocations
   produced it.

## Load-bearing regression tests

Several tests are explicitly named as guarding a specific prior defect or
trimming pass rather than a general feature:
`test_phase_b_identity_prompt_load_bearing.py`,
`test_a2_tool_description_load_bearing.py`,
`test_trim2_tool_text_load_bearing.py`,
`test_docket_doctrine_load_bearing_gaps.py`. These pin exact prompt/tool-text
content and tool counts (`test_mcptool.py` pins the current tool count) —
treat a failure in one of these as "you changed load-bearing text/count," not
as a flaky test to retry past.

`msgvis.py` and similar drift guards grep source for expressions they mirror
and fail loudly if source and test diverge, so correctness in message
visibility and turn lifecycle is verified against literal source-expression
mirroring in addition to behavioral tests.

## Frontend browser probes

The frontend test suite (`frontend/tests/`) supplements conventional
`.test.tsx` component tests with **browser probes** — `.py` scripts that
drive a real browser against a built fixture to exercise canvas behavior
jsdom cannot (transform math, pointer capture, snap alignment, modal
stacking). See [Frontend architecture](../architecture/frontend.md#testing)
for the pattern; `npm run typecheck` is the actual type-safety gate, since
`vite build` does not typecheck.

## What to check when changing a major area

- **Ledger/credits/mail** (`ledger.py`, `supervisor.py`): read
  `docs/ARCHITECTURE.md`'s "Ledger & credits" and "Mail & delivery" sections
  first; run the backend baseline at your tip before and after.
- **Provider tiers, tool text, system prompt**: check
  `test_mcptool.py`'s pinned tool count and the load-bearing prompt tests
  above.
- **Canvas geometry/coordinate spaces**: check the browser probes relevant to
  the touched module (pin/snap, modal stacking, card layout) rather than
  relying on component tests alone.
- **Configuration/deployment surface**: cross-check against
  [Deployment and configuration](deployment-and-config.md) and
  `docs/frozen-deployment.md`'s verification boundary for anything touching
  the frozen profile.

## Related pages

- [Backend architecture](../architecture/backend.md) and
  [Frontend architecture](../architecture/frontend.md) — the systems these
  registers govern.
- [Deployment and configuration](deployment-and-config.md) — where the
  frozen profile and kiosk ceilings this section's discipline protects are
  configured.
