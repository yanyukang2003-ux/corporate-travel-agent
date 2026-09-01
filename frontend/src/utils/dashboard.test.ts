import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import type { MetricResult } from '../api/types.ts'
import {
  formatMetric,
  formatSeconds,
  metricSample,
  utilisation,
  utilisationTone,
  whereaboutsLabel,
} from './dashboard.ts'

const measured = (value: number, unit: string, denominator = 10, numerator = 3): MetricResult => ({
  status: 'measured',
  value,
  numerator,
  denominator,
  unit,
  exposure_note: null,
  confidence_note: null,
})

describe('formatMetric', () => {
  it('shows rates as percentages and other units with their name', () => {
    assert.equal(formatMetric(measured(0.256, 'rate')), '25.6%')
    assert.equal(formatMetric(measured(3, 'days')), '3 天')
    assert.equal(formatMetric(measured(12.34, 'minutes')), '12.3 分钟')
    assert.equal(formatMetric(measured(7, 'count')), '7')
    assert.equal(formatMetric(measured(0.02, 'ratio')), '+2.0%')
    assert.equal(formatMetric(measured(-0.05, 'ratio')), '-5.0%')
    assert.equal(formatMetric(measured(2.3, 'calls')), '2.3 次')
    assert.equal(formatMetric(measured(0, 'rounds')), '0 轮')
    assert.equal(formatMetric(measured(5400, 'seconds')), '1.5 小时')
  })

  it('shows seconds at a readable granularity', () => {
    assert.equal(formatSeconds(42), '42 秒')
    assert.equal(formatSeconds(90), '1.5 分钟')
    assert.equal(formatSeconds(7200), '2.0 小时')
  })

  it('says a metric is unmeasured instead of printing zero', () => {
    assert.equal(formatMetric(undefined), '测不出来')
    assert.equal(formatMetric({ ...measured(0, 'rate'), status: 'unavailable', value: null }), '测不出来')
  })
})

describe('metricSample', () => {
  it('shows the fraction behind a rate and the sample size otherwise', () => {
    assert.equal(metricSample(measured(0.3, 'rate', 10, 3)), '3 / 10')
    assert.equal(metricSample(measured(4, 'days', 8)), '样本 8')
    assert.equal(
      metricSample({ ...measured(0, 'rate'), status: 'unavailable', confidence_note: '没有改期任务' }),
      '没有改期任务',
    )
  })
})

describe('utilisation', () => {
  it('clamps to [0, 1] and returns null without ledger data', () => {
    assert.equal(utilisation('250', '1000'), 0.25)
    assert.equal(utilisation('1500', '1000'), 1)
    assert.equal(utilisation(null, '1000'), null)
    assert.equal(utilisation('10', '0'), null)
  })

  it('maps ratios to tones', () => {
    assert.equal(utilisationTone(0.2), 'ok')
    assert.equal(utilisationTone(0.85), 'warn')
    assert.equal(utilisationTone(1), 'over')
    assert.equal(utilisationTone(null), 'unknown')
  })
})

describe('whereaboutsLabel', () => {
  it('names every status in plain language', () => {
    assert.equal(whereaboutsLabel('IN_TRANSIT'), '在途')
    assert.equal(whereaboutsLabel('AT_DESTINATION'), '在目的地')
    assert.equal(whereaboutsLabel('UPCOMING'), '未出发')
    assert.equal(whereaboutsLabel('COMPLETED'), '已结束')
  })
})
