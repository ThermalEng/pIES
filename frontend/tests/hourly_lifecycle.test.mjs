/** Serial complete reads and cancellation use the production feature/HTTP chain. */
import assert from 'node:assert/strict'
import { readHourly } from '../src/features/results/api.ts'

const series = Array.from({ length: 17 }, (_, i) => ({
  series_id: `device${i}.power`, kind: 'device', instance_id: `device${i}`,
  variable_id: 'power', unit: 'kW',
}))
const rows = 6
const ref = { object_id: 9, fields: series.map(s => s.series_id), rows, series }
const json = value => new Response(JSON.stringify(value), { status: 200 })
const values = (index, start, end) => Array.from({ length: end - start }, (_, k) => index * 10 + start + k)

// A paginated 17-series response must have only one in-flight page request.
{
  let active = 0
  let peak = 0
  const calls = []
  globalThis.fetch = async (url) => {
    if (!String(url).includes('/hourly')) return json({ result: { hourly_refs: [ref] } })
    active++
    peak = Math.max(peak, active)
    const q = new URL(String(url), 'http://test').searchParams
    const field = q.get('field')
    const start = Number(q.get('start'))
    const end = Math.min(rows, start + 2)
    calls.push([field, start])
    await new Promise(resolve => setTimeout(resolve, 0))
    active--
    return json({ values: values(series.findIndex(s => s.series_id === field), start, end),
      next_start: end < rows ? end : null })
  }
  const result = await readHourly(1, 2)
  assert.equal(result.absent, false)
  assert.equal(peak, 1)
  assert.equal(calls.length, 51)
  assert.deepEqual(result.series, series)
  for (const [index, meta] of series.entries()) {
    assert.deepEqual(result.flows[meta.series_id], values(index, 0, rows))
    assert.deepEqual(calls.slice(index * 3, index * 3 + 3), [0, 2, 4].map(start => [meta.series_id, start]))
  }
  console.log('[PASS] 17 complete series, 51 pages, peak concurrency = 1')
}

// Passing an already-aborted signal must reach fetch as aborted, not restart work.
{
  const controller = new AbortController()
  controller.abort()
  globalThis.fetch = async (_url, { signal }) => {
    assert.equal(signal.aborted, true)
    throw new DOMException('Aborted', 'AbortError')
  }
  await assert.rejects(readHourly(1, 2, undefined, controller.signal))
  console.log('[PASS] pre-aborted caller signal stays aborted')
}

// An in-flight cancellation stops the current page and never starts other pages/series.
{
  const controller = new AbortController()
  let entered
  const ready = new Promise(resolve => { entered = resolve })
  let pages = 0
  globalThis.fetch = async (url, { signal }) => {
    if (!String(url).includes('/hourly')) return json({ result: { hourly_refs: [ref] } })
    pages++
    return new Promise((_resolve, reject) => {
      signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true })
      entered()
    })
  }
  const reading = readHourly(1, 2, undefined, controller.signal)
  await ready
  controller.abort()
  await assert.rejects(reading)
  assert.equal(pages, 1)
  console.log('[PASS] in-flight cancellation stops the group')
}

// Cancellation while consuming a completed page also stops the next page.
{
  const controller = new AbortController()
  let pages = 0
  globalThis.fetch = async (url) => {
    if (!String(url).includes('/hourly')) return json({ result: { hourly_refs: [ref] } })
    pages++
    const response = json({ values: [0, 1], next_start: 2 })
    const text = response.text.bind(response)
    response.text = async () => { controller.abort(); return text() }
    return response
  }
  await assert.rejects(readHourly(1, 2, undefined, controller.signal))
  assert.equal(pages, 1)
  console.log('[PASS] cancellation between pages does not start another request')
}

// A real HTTP failure rejects the whole group, without continuing other series.
{
  let pages = 0
  globalThis.fetch = async (url) => {
    if (!String(url).includes('/hourly')) return json({ result: { hourly_refs: [ref] } })
    pages++
    return new Response('failed', { status: 500 })
  }
  await assert.rejects(readHourly(1, 2))
  assert.equal(pages, 1)
  console.log('[PASS] failed page stops the group, not partial success')
}
