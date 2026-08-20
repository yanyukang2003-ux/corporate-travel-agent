import assert from 'node:assert/strict'
import { describe, it } from 'node:test'
import {
  EXAMPLE_TRIP_MESSAGE,
  clarificationRetry,
  clearComposerDraft,
  initialComposerMessage,
  isExampleTripMessage,
  readClarificationDraft,
  readComposerDraft,
  writeClarificationDraft,
  writeComposerDraft,
} from './draft.ts'

class MemoryStorage {
  private readonly data = new Map<string, string>()

  getItem(key: string): string | null {
    return this.data.has(key) ? this.data.get(key)! : null
  }

  setItem(key: string, value: string): void {
    this.data.set(key, value)
  }

  removeItem(key: string): void {
    this.data.delete(key)
  }
}

describe('composer draft', () => {
  it('does not start with the demo template', () => {
    const storage = new MemoryStorage()
    const initial = initialComposerMessage(storage)
    assert.equal(initial, '')
    assert.equal(isExampleTripMessage(initial), false)
    assert.notEqual(initial, EXAMPLE_TRIP_MESSAGE)
  })

  it('restores the user draft after a remount, never the demo', () => {
    const storage = new MemoryStorage()
    writeComposerDraft('周四从杭州去广州，当天往返，不要酒店', storage)

    const remounted = initialComposerMessage(storage)
    assert.equal(remounted, '周四从杭州去广州，当天往返，不要酒店')
    assert.equal(isExampleTripMessage(remounted), false)
  })

  it('clears an empty draft so remount stays empty', () => {
    const storage = new MemoryStorage()
    writeComposerDraft('周四从杭州去广州', storage)
    writeComposerDraft('   ', storage)
    assert.equal(readComposerDraft(storage), '')
    assert.equal(initialComposerMessage(storage), '')
  })

  it('survives a failed submit then remount', () => {
    const storage = new MemoryStorage()
    writeComposerDraft('补充：客户公司在徐家汇', storage)
    const afterErrorRemount = initialComposerMessage(storage)
    assert.equal(afterErrorRemount, '补充：客户公司在徐家汇')
    clearComposerDraft(storage)
    assert.equal(initialComposerMessage(storage), '')
  })
})

describe('clarification retry', () => {
  it('retries the failed supplement instead of the demo template', () => {
    const retry = clarificationRetry({
      originalInstruction: EXAMPLE_TRIP_MESSAGE,
      lastFailedAnswer: '客户公司在徐家汇，周三下午前必须到',
      hasSubmitError: true,
    })
    assert.equal(retry.mode, 'failed_supplement')
    assert.equal(retry.text, '客户公司在徐家汇，周三下午前必须到')
    assert.notEqual(retry.text, EXAMPLE_TRIP_MESSAGE)
    assert.equal(retry.label, '重试刚才的补充')
  })

  it('falls back to the original instruction when nothing failed', () => {
    const retry = clarificationRetry({
      originalInstruction: '下周四从成都去深圳见客户',
      lastFailedAnswer: '',
      hasSubmitError: false,
    })
    assert.equal(retry.mode, 'original')
    assert.equal(retry.text, '下周四从成都去深圳见客户')
  })

  it('persists a failed supplement across remount', () => {
    const storage = new MemoryStorage()
    writeClarificationDraft('task-1', {
      customAnswers: { details: '客户公司在徐家汇' },
      lastFailedAnswer: '客户公司在徐家汇',
    }, storage)
    const restored = readClarificationDraft('task-1', storage)
    assert.deepEqual(restored, {
      customAnswers: { details: '客户公司在徐家汇' },
      lastFailedAnswer: '客户公司在徐家汇',
    })
    const retry = clarificationRetry({
      originalInstruction: EXAMPLE_TRIP_MESSAGE,
      lastFailedAnswer: restored?.lastFailedAnswer ?? '',
      hasSubmitError: true,
    })
    assert.equal(retry.text, '客户公司在徐家汇')
  })
})
