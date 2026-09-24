// actlabel.test.tsx — the live-activity label on a busy agent's card.
//
// USER BUG 2026-08-26: "when an agent is actively working, their zoomed out
// status text sometimes overflows and flows off of their card, going to the
// side or below it." The tool name was a BARE TEXT NODE inside a flex row —
// an anonymous flex item, which cannot carry `text-overflow`, and whose
// automatic minimum size is its longest unbreakable word. Tool names are
// nothing but long unbreakable words, so the row wrapped to a second line and
// that line still ran past the border: measured at +50.95px on a card with
// 108px to give, far enough to land on the neighbouring card.
//
// User ruling 2026-09-07 06:28Z: the label no longer names the tool at all —
// a busy card just says "Active" (the SYSTEM-OBSERVED turn state), with the
// spin icon carrying the rest. That retires the overflow risk this file used
// to pin (no more unbreakable tool-name text) and, with it, the `Activity`
// component and its `.actlabel`/`.actgear` markup this file used to mount
// directly — the card's busy state is `AgentWorkstate` now (`.sq-workstate`),
// and `node.activity`/`ActivityInfo` moved on to the Progress panel's own
// per-agent activity fold (progress.tsx), which is out of scope here. The one
// contract that still matters: the card says "Active" on a busy card, and
// only there.
//
// Run:  cd frontend && node tests/run.mjs actlabel

import { mountView } from './harness'
import test from 'node:test'
import assert from 'node:assert/strict'
import { NodeSquare } from '../src/canvas/cards'
import type { CanvasNode } from '../src/canvas/shared'
import type { OpResult } from '../src/types'

const noop = () => {}
const op = () => Promise.resolve({} as OpResult)

function node(id: string, extra: Partial<CanvasNode> = {}): CanvasNode {
  return {
    id, state: 'live', tier: 'opus', children: [], seat: 1, grant: 0, free: 0,
    scope: { tools: {}, add_dirs: [] }, model_id: 'opus', ...extra,
  }
}

const card = (nd: CanvasNode, lod: 'mini' | 'norm' = 'norm') =>
  mountView(
    <NodeSquare node={nd} pos={{ x: 0, y: 0 }} lod={lod} focused={false}
      dragging={false} isDrop={false} seats={{ used: 1, total: 4 }}
      map={new Map([[nd.id, nd]])} op={op} slug="org" toast={noop}
      pxc={1} zoom={1} compactAt={0.8} pub={false} maxTop={0}
      kioskRemaining={null} cascadeAlloc
      onSpawn={noop} onSpawnSide={noop} onSpawnTop={noop} onConfig={noop}
      onInbox={noop} onLineage={noop} onOpenDoc={noop} onRecenter={noop}
      onJump={noop} onMailLink={noop} onDragStart={noop} onDragMove={noop}
      onDragEnd={noop} onDragCancel={noop} />,
    (el) => el)

// Where the label is allowed to appear, and what it says now that it no
// longer names the tool. jsdom applies no stylesheet, so the mini-LOD
// collapse (a CSS-only rule, `.sq.mini .sq-head`) is a probe concern, not a
// DOM-presence one — only the busy/idle contract is checked here.
test('the text label appears on a busy card, and only there', async () => {
  const busy = await card(node('a1', { busy: true }))
  const text = busy.el.querySelector('.sq-workstate .working')
  assert.ok(text, 'no active indicator on a busy card')
  assert.equal(text!.textContent, 'Active')
  assert.ok(busy.el.querySelector('.sq-workstate .cc-spin'),
    'the spin icon is what carries the busy tell now — it must not be lost')

  const idle = await card(node('a2'))
  assert.equal(idle.el.querySelector('.sq-workstate .working'), null,
    'an idle agent is not doing anything, so it claims nothing')
})

test('a Sol card carries the OpenAI theme and the S tier tag', async () => {
  const codex = await card(node('codex-sol', {
    tier: 'sol', model_id: 'gpt-5.6-sol', seat: 5,
  }))
  const sq = codex.el.querySelector('.sq')
  assert.ok(sq?.classList.contains('prov-openai'),
    'the provider theme class is missing from the Codex card')
  assert.ok(sq?.classList.contains('tier-sol'),
    'the independent Sol tier stripe class is missing from the Codex card')
  assert.equal(sq?.querySelector('.tier')?.textContent, 'S')
})
