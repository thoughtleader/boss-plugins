import { test, expect, mock } from 'claude-code/testing'
import type { On } from 'claude-code'

const NOW = Date.parse('2026-11-11T19:10:00Z')
const RUN = 'fixture'
const PANE = {
  plugin: 'boss', component: 'Pane', requestId: 'boss-meter',
  props: { title: 'Boss meter', isFocused: false, bodyColumns: 80, placement: 'dock', scroll: { offset: 0, bodyRows: 40 }, view: {} },
} as const
const iso = (ago: number) => new Date(NOW - ago * 1000).toISOString()
const snapshot = (owner: string) => ({
  schema: 'boss.progress/1', tool: 'orchestrator', run_id: RUN, title: 'Native meter fixture',
  owner: { session_id: owner }, state: 'running', started_at: iso(380), updated_at: iso(1),
  items: [
    { id: 'wave:1', kind: 'wave', label: 'wave 1', state: 'running' },
    { id: 'row:1', parent: 'wave:1', kind: 'row', label: 'Setup', state: 'done', started_at: iso(380), ended_at: iso(128), meta: { tokens: 16000 } },
    { id: 'row:2', parent: 'wave:1', kind: 'row', label: 'Validate', state: 'running', started_at: iso(128) },
    { id: 'row:3', parent: 'wave:1', kind: 'gate', label: 'Review', state: 'queued' },
    { id: 'row:4', parent: 'wave:1', kind: 'row', label: 'Deliver', state: 'queued' },
  ], attention: [],
})

function world(on: On, owner: string, data = snapshot(owner)) {
  mock.env(on, { HOME: '/h', BOSS_PROGRESS_DIR: '/r' })
  const clock = mock.clock(on, { now: NOW })
  on('session.id', () => ({ value: 'mine' }))
  on('fs.list', (_, e) => ({ value: e.path === '/r/runs'
    ? [{ name: 'orchestrator', kind: 'dir', size: 0, mtimeMs: NOW, isLink: false }]
    : e.path === '/r/runs/orchestrator'
      ? [{ name: RUN, kind: 'dir', size: 0, mtimeMs: NOW, isLink: false }] : [] }))
  on('fs.read', (_, e) => {
    if (e.path !== `/r/runs/orchestrator/${RUN}/progress.json`) throw new Error('ENOENT')
    return { value: JSON.stringify(data) }
  })
  on('command.register', () => ({ value: { command: 'boss-meter' } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('ui.status', () => ({ value: undefined }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  return clock
}

test('compact progress and details render on terminal and VS Code', async ($, on) => {
  const clock = world(on, 'mine')
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await clock.settle()
  await $.command.run({ command: 'boss-meter', args: '', origin: { kind: 'composer' }, presentation: { isFullscreen: true, columns: 100 } })
  for (const surface of ['terminal', 'vscode'] as const) {
    const view = await $.ui.mount({ ...PANE, surface })
    expect((await view.find({ type: 'Text', text: /▰▱▱▱/ }))?.text).toContain('▰▱▱▱')
    expect((await view.find({ type: 'Text', text: /16K/ }))?.text).toContain('16K')
    await view.press({ key: 'toggle-details' })
    expect((await view.find({ type: 'Text', text: /reported/ }))?.text).toContain('reported')
    await view.press({ key: 'toggle-details' })
    await view.unmount()
  }
})

test('a run owned by another session requires an explicit selection', async ($, on) => {
  const clock = world(on, 'someone-else')
  await $.session.start({ cwd: '/', surface: 'vscode', isInteractive: true })
  await clock.settle()
  const view = await $.ui.mount({ ...PANE, surface: 'vscode' })
  expect((await view.find({ type: 'Text', text: /Choose a run/ }))?.text).toBe('Choose a run')
  await view.press({ key: 'choose-orchestrator/fixture' })
  expect((await view.find({ type: 'Text', text: /▰▱▱▱/ }))?.text).toContain('▰▱▱▱')
  await view.unmount()
})

test('artifact report renders inside mixed prose with frozen task details on terminal and VS Code', async ($, on) => {
  const task = (id: string, label: string, state: string) => ({ id, label, state, wave: 'B', elapsedMs: 128000, tokens: 16000, partialTokens: false, model: 'sonnet', effort: 'high', result: state === 'done' ? 'review.md' : null, sha: null, detail: null, action: null })
  const now = task('2', 'Validate', 'running'), next = task('3', 'Review', 'queued'), recent = task('1', 'Setup', 'done')
  const meter = {
    current: { label: 'Wave 2/3', done: 1, total: 4, elapsedMs: 128000, tokens: 16000, partialTokens: true, running: 2, state: 'running', bar: '▰▱▱▱', ticking: true },
    total: { label: 'Total', done: 4, total: 7, elapsedMs: 380000, tokens: 44000, partialTokens: true, running: 2, state: 'running', bar: '▰▰▰▰▱▱▱', ticking: true },
    waves: { label: 'Waves', done: 1, total: 3, elapsedMs: 380000, tokens: 44000, partialTokens: true, running: 1, state: 'running', bar: '▰▱▱', ticking: true },
    attention: [], tokenNote: 'Tokens are reported, not billing estimates.',
    tasks: { now: [now], next: [next], recent: [recent], all: [recent, now, next], nowCount: 1, nextCount: 1, recentCount: 1 },
  }
  const hiddenInstruction = 'Synthesis: one drift table grouped by the shared rule each drift breaks, with proposed fix rows for stage 3'
  const hidden = { ...task('long', hiddenInstruction, 'done'), result: 'synthesis.md', instructions: hiddenInstruction + '. Preserve the supporting evidence.' }
  meter.tasks.recent.push(hidden); meter.tasks.all.push(hidden); meter.tasks.recentCount++
  const hash = 'a'.repeat(64)
  const reportText = '```text\n[W]  ▰▰▰▰▱▱▱▱▱▱▱▱  06:20  44K\n[T]  ▰▰▰▱▱▱▱▱▱▱▱▱  02:08  16K\nNOW  Validate\nNEXT Review\n```'
  const artifact = { schema: 'boss.report/1', run: 'fixture', created_at: '2026-11-11T19:10:00Z', columns: 72, meter, text: reportText }
  mock.env(on, { HOME: '/h', BOSS_PROGRESS_DIR: '/r' })
  on('fs.read', (_, e) => {
    if (e.path !== `/r/reports/${hash}.json`) throw new Error('Unexpected artifact read')
    return { value: JSON.stringify(artifact) }
  })
  const prefix = 'Merged the change. **Here is progress.**\n\n'
  const suffix = '\n\nThe remaining work is unchanged.'
  const text = prefix + reportText + `\n\n[DETAILS](file:///r/reports/${hash}.md)` + suffix
  for (const surface of ['terminal', 'vscode'] as const) {
    const view = await $.ui.mount({ plugin: 'boss', component: 'AssistantMessage', requestId: 'fixture-message', surface, viewport: { columns: 80, rows: 30, isFullscreen: true }, props: { text, isFirstOfReply: true } })
    expect((await view.find({ type: 'Text', text: /▰▱▱▱/ }))?.text).toContain('▰▱▱▱')
    expect(await view.find({ type: 'Markdown', key: 'report-prefix' })).toBeUndefined()
    expect(await view.find({ type: 'Markdown', key: 'report-suffix' })).toBeUndefined()
    expect(await view.find({ key: 'summary-done' })).toBeDefined()
    expect(await view.find({ key: 'summary-now' })).toBeDefined()
    expect(await view.find({ key: 'summary-next' })).toBeDefined()
    expect((await view.find({ type: 'Text', text: /^➜$/ }))?.props.color).toBe('#3B82F6')
    expect((await view.find({ type: 'Text', text: /^⮡$/ }))?.props.color).toBe('#3B82F6')
    expect(await view.find({ type: 'Text', text: /Synthesis:/ })).toBeUndefined()
    await view.press({ key: 'inline-details' })
    expect((await view.find({ type: 'Text', text: /Synthesis:/ }))?.text).toContain('Synthesis:')
    await view.press({ key: 'task-toggle-long' })
    expect((await view.find({ type: 'Text', text: /supporting evidence/ }))?.text).toContain('Preserve the supporting evidence.')
    await view.press({ key: 'task-toggle-long' })
    expect((await view.find({ type: 'Markdown', key: 'report-prefix' }))?.props.text).toBe(prefix)
    expect((await view.find({ type: 'Markdown', key: 'report-suffix' }))?.props.text).toBe(suffix)
    expect(await view.find({ key: 'report-prefix-bullet' })).toBeDefined()
    expect(await view.find({ key: 'details-current' })).toBeDefined()
    expect(await view.find({ key: 'details-queued' })).toBeDefined()
    expect(await view.find({ key: 'details-results' })).toBeDefined()
    expect(await view.find({ type: 'Text', text: /review.md/ })).toBeUndefined()
    await view.press({ key: 'task-toggle-1' })
    expect((await view.find({ type: 'Text', text: /review.md/ }))?.text).toContain('review.md')
    await view.press({ key: 'task-toggle-1' })
    expect((await view.find({ type: 'Text', text: /billing estimates/ }))?.text).toContain('reported')
    await view.press({ key: 'inline-details' })
    await view.unmount()
  }
  const plain = await $.ui.mount({ plugin: 'boss', component: 'AssistantMessage', requestId: 'ordinary', surface: 'vscode', props: { text: 'Keep this unrelated explanation.', isFirstOfReply: true } })
  expect(await plain.find({ key: 'boss-meter-inline' })).toBeUndefined()
  await plain.unmount()
})


test('thirteen task ticks stay on one native row with unfinished work visible', async ($, on) => {
  const data = {
    ...snapshot('mine'),
    items: [
      { id: 'wave:1', kind: 'wave', label: 'wave 1', state: 'running' },
      ...Array.from({ length: 13 }, (_, i) => ({ id: `row:${i + 1}`, parent: 'wave:1', kind: 'row', label: `Task ${i + 1}`, state: i < 12 ? 'done' : 'running', started_at: iso(128), ...(i < 12 ? { ended_at: iso(60) } : {}) })),
    ],
  }
  const clock = world(on, 'mine', data)
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await clock.settle()
  await $.command.run({ command: 'boss-meter', args: '', origin: { kind: 'composer' }, presentation: { isFullscreen: true, columns: 44 } })
  for (const surface of ['terminal', 'vscode'] as const) {
    const view = await $.ui.mount({ ...PANE, surface, props: { ...PANE.props, bodyColumns: 44 } })
    const ticks = await view.find({ type: 'Text', text: /^▰{11}▱$/ })
    expect(ticks?.text).toBe('▰'.repeat(11) + '▱')
    await view.press({ key: 'toggle-details' })
    expect((await view.find({ type: 'Text', text: /12 completed, 1 unfinished/ }))?.text).toContain('12 completed, 1 unfinished')
    await view.press({ key: 'toggle-details' })
    await view.unmount()
  }
})
