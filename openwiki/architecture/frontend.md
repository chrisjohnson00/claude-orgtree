---
type: Architecture Overview
title: Frontend architecture — the office-room canvas
description: How the React/TypeScript/Vite frontend renders the org as a pannable, zoomable canvas — coordinate spaces, the camera engine, the desk's inverted-scale regime, pinned windows, mobile mode, and the API/live-update layer.
tags: [frontend, architecture, react, canvas, typescript]
resource: frontend/src/canvas/
---

# Frontend architecture — the office-room canvas

`frontend/src/` is a React + TypeScript + Vite application rendering the org
as an infinite, pannable/zoomable "office-room" canvas: agent cards beneath a
fixed root anchor ("the eye"), curved wires for reporting lines, dotted
peer links, and animated "sparks" for mail in flight. The full interaction
manual is [`docs/ui-guide.md`](../../docs/ui-guide.md); this page explains how
the canvas is actually built.

## File organization (`frontend/src/canvas/`)

Originally one large `Canvas.tsx`, now split into focused modules re-exported
through a thin barrel (`Canvas.tsx`):

| Module | Responsibility |
|---|---|
| `shared.ts` | Pure types, geometry, constants: view types, tier tables, `layout()`/`flatten()` tree math, springs, the markdown pipeline, small shared hooks |
| `OrgCanvas.tsx` | The canvas core — camera (pan/zoom/springs/follow), tree layout orchestration, wires/sparks, drag & re-parenting, retired/crowd piles, HUD, modal wiring |
| `cards.tsx` | `UserNode`/`DraftNode`/`NodeSquare`, the switchboard, the credit bar |
| `desk.tsx` | The zoomed-in per-agent chat surface — transcript, live streaming rows, activity/turn-state badges |
| `deskhosts.tsx` | A stable desk-identity registry so a desk survives reparenting/renaming without remounting |
| `pins.tsx` / `pinSnap.ts` | Screen-space pinned/movable windows, detached from the world transform |
| `clearRect.ts` | Pure geometry: largest-empty-rectangle search so camera commands avoid pinned windows |
| `mail.tsx`, `docket.tsx`, `modals.tsx`, `accounts.tsx`, `gallery.tsx`, `reflinks.tsx` | Feature-specific overlays: mailbox, the docket, dialogs, the accounts panel, the agent gallery, cross-reference links |

`App.tsx` is the top-level shell (routing between the canvas, kill switch,
git workspace, freeze log, and mobile views); `api.ts` is the REST client and
`livebus.ts` carries live update events into the rest of the app.

## The coordinate-space model

The core architectural idea, documented at the top of `pins.tsx`:

- **World px** — where cards live. `.space` (inside `.viewport`) carries
  `transform: translate(view.x, view.y) scale(view.z)`. A card at world
  `(p.x, p.y)` renders on screen at `(p.x*z + view.x, p.y*z + view.y)`.
- **Viewport px** — the `.viewport` box's own coordinate system. HUD chrome,
  edge-jump cards, the tray, and pinned windows all live here as screen-space
  siblings of `.space`, immune to pan/zoom *by construction* (outside the
  transform) rather than by compensating math.

`clearRect.ts` is pure geometry (no React/DOM) computing the single largest
empty viewport rectangle not covered by pinned windows; every camera command
(focus, fit, switchboard) uses it uniformly.

## The camera engine

`view = {x, y, z}` state plus a `viewRef` mirror lets RAF loops and handlers
read the current view without stale closures. Critically-damped springs
(`SPRING_K`/`SPRING_C`) drive each node's screen position toward `layout()`
targets; a "follow" mode can ride the camera with a specific node, yielding to
manual pan/pinch. Pointer/pinch/drag state lives in refs
(`panRef`/`pointersRef`/`pinchRef`/`nodeDrag`), not React state, to avoid
re-render cost on every pointer move; `setFrame` is the escape hatch used to
force a repaint when only ref-held state changed. **The eye (root/user) has a
fixed world position** so the coordinate space does not shift under it as the
tree grows.

`layout()` in `shared.ts` is a recursive tidy-tree placement (children
averaged for x, depth for y) over a `CanvasNode` tree from `flatten()` plus
`withDraftTree()`, which injects an uncommitted hire draft into the same tree
shape so it renders identically to a real node. Org-chart lines are SVG paths
built from segment descriptors (`treeSeg`/`peerSeg`/`audSeg`); mail "sparks"
and audience-grant line animations are driven by a self-terminating
`requestAnimationFrame` loop.

## The desk — an inverted-scale regime

A desk (the zoomed chat surface for one agent) is authored at a virtual
900px panel, then counter-scaled down (`scale(0.13333 × deskDpi)`) to fit the
120px card interior — because browsers clamp small font sizes *up*, which
would explode a naively-small layout. `DESK_SCALE` (0.13333) is a single
source of truth shared between `cards.tsx` (the eye desk) and the desk CSS,
with the invariant `900 × 0.13333 = 120` asserted in comments. `--desk-dpi` is
a user text-size dial that shrinks the virtual space while scaling up
proportionally, preserving the same 120px footprint. A parallel counter-scale
variable (`--invz`/`--invzf`) keeps HUD chrome and hire-chip controls
screen-constant size regardless of camera zoom.

`deskhosts.tsx` keeps a desk's React subtree (live subscriptions, scroll
position, in-flight composer drafts) attached to a stable "host" across
rename/reparent/re-render, so those actions never force a remount; a desk's
identity key includes an asserted, non-negotiable generation number, because
two composers silently sharing one `localStorage` key was an actual bug
class.

## Pinning and detaching desks

`pins.tsx`/`pinSnap.ts` implement OS-window-like pinned desks: drag a desk off
the world into a movable/resizable floating window immune to canvas pan/zoom,
persisted per-org in `localStorage`. Snap-to-edge/neighbor logic lives in the
pure, testable `pinSnap.ts`. **Invariant: one live desk per agent** — a pinned
node is excluded from camera focus so a second desk can never mount for the
same node and fight over the same composer-draft storage key. Desks can also
fully detach into separate browser windows (`popout.tsx`, `windowlife.ts`),
sharing notification portals so toasts and restart banners reach whichever
window is relevant.

## Mail and the docket in the UI

`mail.tsx` renders the webmail-style inbox (per-mail read tracking, sent
folder, the switchboard's side-by-side live chats) over the backend's
three-store mail model; `docket.tsx` renders the work-item list and detail
pane over the backend docket. Both are product concepts documented in
[Mail and the docket](../domain/mail-and-work.md) rather than duplicated
here.

## Mobile mode

Mobile is not a responsive breakpoint — it is an OS allowlist
(`mobile.tsx`) evaluated once at boot and stamped as an `html.mobile` class.
Below a size threshold, the infinite-canvas desk degrades to a full-screen
1:1 "sheet" rather than attempting the counter-scale trick at an unreadable
size (`docs/mobile-spec.md`: "the spatial canvas does not survive at 375px as
a work surface; it survives as a locator"). Backlogged for a deeper pass — see
[quickstart backlog](../quickstart.md#backlog).

## API and live-update layer

`api.ts` is the REST client used throughout the canvas; `livebus.ts` carries
live update events (new mail, turn state changes, backend-restart notices)
into components without a global store. The project's own stated principle
(from a retained internal audit, `docs/attic/state-architecture-review.md`):
**"a repair mechanism must never be gated on the data it repairs"** and
**"the websocket is an optimization, not a requirement — nothing on screen
may depend on having caught an event."** Prefer polling/refetch fallbacks
over assuming a live event will always arrive.

## Documented gotchas

From `docs/ARCHITECTURE.md`'s frontend section — read the source there before
touching canvas internals:

- Nothing inside `.space` may use `position: fixed` (the CSS transform makes
  it the containing block); portal to `document.body` instead.
- The viewport must never natively scroll — `scrollLeft`/`scrollTop` are
  zeroed every frame; an unguarded `.focus()` can shear the whole HUD
  off-screen.
- No CSS transitions on spring-animated geometry (wire paths, node
  transforms) — an eased transition stacked on per-frame spring math causes
  visible drift.
- Background pan calls `setPointerCapture` on the viewport, so any
  screen-space control layered over the canvas must `stopPropagation` on
  `pointerdown` or its clicks silently swallow.
- `vite build` does not typecheck — `npm run typecheck` is the actual gate.

## Testing

`frontend/tests/` mixes conventional component tests (`.test.tsx`) with
**browser probes** — `.py` scripts (e.g. `modalpin_probe.py`,
`stack_browser_probe.py`, `agentgallery-browser.py`) that drive a real
browser against a built fixture to verify pixel-level canvas behavior that
jsdom cannot exercise (transform math, pointer capture, snap alignment). See
[Testing and governance](../operations/testing-and-governance.md) for how
these fit with the backend suite and the test-baseline caveat.
