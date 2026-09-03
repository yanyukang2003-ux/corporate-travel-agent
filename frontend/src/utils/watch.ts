/**
 * 订好之后的追踪：观察对象、航班动态、变更事件怎么给人看。
 *
 * 纯函数，不碰 DOM、不调 API，`watch.test.ts` 直接跑在 node 上。
 */
import type { ChangeImpactVerdict, FlightObservation, FlightStatusKind, TripAggregate, TripEvent, TripStatus, WatchLeg } from '../api/types.ts'

/** 差旅状态 → 给人看的话。 */
export function tripStatusLabel(status: TripStatus | string): string {
  switch (status) {
    case 'PLANNED': return '规划中'
    case 'BOOKED': return '已订 · 观察中'
    case 'CHANGE_REQUESTED': return '改期进行中'
    case 'REBOOKED': return '已改订 · 观察中'
    case 'CANCELLED': return '已取消'
    default: return status
  }
}

export function tripStatusTone(status: TripStatus | string): string {
  switch (status) {
    case 'BOOKED':
    case 'REBOOKED': return 'green'
    case 'CHANGE_REQUESTED': return 'orange'
    case 'CANCELLED': return 'dark'
    default: return 'gray'
  }
}

export function flightStatusLabel(status: FlightStatusKind | string): string {
  switch (status) {
    case 'SCHEDULED': return '按计划'
    case 'DELAYED': return '延误 / 改时'
    case 'CANCELLED': return '已取消'
    case 'DEPARTED': return '已起飞'
    case 'LANDED': return '已落地'
    case 'UNKNOWN': return '查不到'
    default: return status
  }
}

export function verdictLabel(verdict: ChangeImpactVerdict | string): string {
  switch (verdict) {
    case 'NO_CHANGE': return '无影响'
    case 'NOTIFY_ONLY': return '仍来得及，已通知'
    case 'REBOOK_REQUIRED': return '需要改期'
    default: return verdict
  }
}

export function verdictTone(verdict: ChangeImpactVerdict | string): string {
  switch (verdict) {
    case 'NOTIFY_ONLY': return 'orange'
    case 'REBOOK_REQUIRED': return 'best'
    default: return 'gray'
  }
}

/** 一段的最新动态；没查过就是 null。 */
export function observationFor(trip: TripAggregate | null, ref_id: string): FlightObservation | null {
  return trip?.watch?.observations?.find((item) => item.ref_id === ref_id) ?? null
}

/** 一段交通的一句话：票号、航线、起降时刻，以及最新动态。 */
export function legLine(leg: WatchLeg, observation: FlightObservation | null): string {
  const base = `${leg.ref_id} ${leg.origin}→${leg.destination} ${clock(leg.depart_at)} → ${clock(leg.arrive_at)}`
  if (!observation) return `${base} · 还没查过动态`
  const parts = [flightStatusLabel(observation.status)]
  if (observation.estimated_arrive_at) parts.push(`预计 ${clock(observation.estimated_arrive_at)} 到`)
  return `${base} · ${parts.join('，')}`
}

/** 变更事件的一句话：类型、谁报的、开出的改期任务。 */
export function eventLine(event: TripEvent): string {
  const head = event.event_type === 'FLIGHT_CHANGED'
    ? `航班变更 ${event.ref_id ?? ''}`.trim()
    : event.event_type === 'MEETING_MOVED'
      ? `会议改期${event.leg_index != null ? `（第 ${event.leg_index + 1} 段）` : ''}`
      : event.event_type === 'TRIP_CANCELLED'
        ? '差旅取消'
        : event.event_type
  const by = event.reported_by.startsWith('flight-status:') ? '航班动态源' : event.reported_by
  const tail = event.opened_task_id ? `，已开改期任务 ${event.opened_task_id.slice(0, 8)}` : ''
  const note = event.note ? ` · ${event.note}` : ''
  return `${head} · ${by} 报${tail}${note}`
}

/** 表单里的本地时刻（datetime-local 的值）+ 时区偏移 → 带时区的 ISO 字符串。 */
export function localInputToIso(value: string, offset = '+08:00'): string | null {
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(value)) return null
  return `${value.length === 16 ? `${value}:00` : value}${offset}`
}

/** 还能不能在这趟差旅上报变更 / 取消。 */
export function canReportChange(trip: TripAggregate | null): boolean {
  return !!trip && (trip.status === 'BOOKED' || trip.status === 'REBOOKED')
}

export function canCancel(trip: TripAggregate | null): boolean {
  return !!trip && trip.status !== 'CANCELLED'
}

function clock(value: string): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(value)
  if (!match) return value
  return `${match[2]}-${match[3]} ${match[4]}:${match[5]}`
}
