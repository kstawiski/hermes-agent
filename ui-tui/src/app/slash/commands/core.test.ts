import { describe, expect, it, vi } from 'vitest'

import { coreCommands } from './core.js'

const steer = coreCommands.find(command => command.name === 'steer')!

const flush = async () => {
  await Promise.resolve()
  await Promise.resolve()
}

const makeContext = (rpc: () => Promise<unknown>) => {
  const enqueue = vi.fn()
  const setHistoryItems = vi.fn()
  const sys = vi.fn()

  const ctx = {
    composer: { enqueue },
    gateway: { rpc },
    guarded: (fn: (value: unknown) => void) => fn,
    sid: 'sid',
    transcript: { setHistoryItems, sys },
    ui: { busy: true }
  }

  return { ctx, enqueue, setHistoryItems, sys }
}

describe('/steer fallback', () => {
  it('queues once when the gateway rejects steering', async () => {
    const { ctx, enqueue, sys } = makeContext(() => Promise.resolve({ status: 'rejected' }))

    steer.run('keep this', ctx as never, '/steer')
    await flush()

    expect(enqueue).toHaveBeenCalledOnce()
    expect(enqueue).toHaveBeenCalledWith('keep this')
    expect(sys).toHaveBeenCalledWith(expect.stringContaining('queued for next turn'))
  })

  it('queues once when the steering RPC fails', async () => {
    const { ctx, enqueue, sys } = makeContext(() => Promise.reject(new Error('offline')))

    steer.run('keep this', ctx as never, '/steer')
    await flush()

    expect(enqueue).toHaveBeenCalledOnce()
    expect(sys).toHaveBeenCalledWith(expect.stringContaining('queued for next turn'))
  })

  it('does not queue accepted steering', async () => {
    const { ctx, enqueue, setHistoryItems, sys } = makeContext(() => Promise.resolve({ status: 'queued' }))

    steer.run('keep this', ctx as never, '/steer')
    await flush()

    expect(enqueue).not.toHaveBeenCalled()
    expect(setHistoryItems).toHaveBeenCalledOnce()
    const update = setHistoryItems.mock.calls[0]![0] as (items: unknown[]) => unknown[]

    expect(update([])).toEqual([{ role: 'user', text: 'keep this' }])
    expect(sys).toHaveBeenCalledWith(expect.stringContaining('next safe point'))
  })
})