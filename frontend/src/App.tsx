/**
 * 澄行企业差旅前端主界面：按角色切换规划、审批、政策与审计视图。
 * 核心是差旅任务状态机驱动的 UI（自然语言 → 澄清/表单 → 方案选择 → 审批）。
 * 本文件仅负责展示与调用 API，不改变后端状态机语义。
 */

import { useEffect, useMemo, useRef, useState } from 'react'
import './App.css'
import { useAuth } from './auth/AuthContext'
import { api, ApiError } from './api/client'
import type {
  ActivePolicy,
  AuditEvent,
  ClarificationQuestion,
  HealthResponse,
  LodgingRequirement,
  StructuredTripCreate,
  TaskState,
  TaskSummary,
  TravelOption,
  TripTask,
  UserIdentity,
} from './api/types'
import { formatTravelDate, formatTravelTime, getStateMeta, parseIsoWallClock } from './utils/state'
import {
  EXAMPLE_TRIP_MESSAGE,
  clarificationRetry,
  clearClarificationDraft,
  clearComposerDraft,
  initialComposerMessage,
  readClarificationDraft,
  writeClarificationDraft,
  writeComposerDraft,
} from './utils/draft'

/** 工作台主视图枚举。 */
type View = 'plan' | 'trips' | 'approvals' | 'policy' | 'audit'
/** 侧栏/页面文案所用的工作区角色。 */
type WorkspaceRole = 'employee' | 'approver' | 'admin'
/** 内联 SVG 图标名称。 */
type IconName =
  | 'spark'
  | 'briefcase'
  | 'check'
  | 'book'
  | 'shield'
  | 'plus'
  | 'bell'
  | 'arrow'
  | 'train'
  | 'plane'
  | 'hotel'
  | 'clock'
  | 'leaf'
  | 'info'
  | 'chevron'
  | 'close'
  | 'search'
  | 'filter'
  | 'download'
  | 'user'
  | 'link'

/** 从用户角色推导工作区角色（admin > 纯 approver > employee）。 */
function workspaceRole(user: UserIdentity): WorkspaceRole {
  if (user.roles.includes('admin')) return 'admin'
  if (user.roles.includes('approver') && !user.roles.includes('employee')) return 'approver'
  return 'employee'
}

/** 按角色返回可见导航视图列表。 */
function allowedViews(user: UserIdentity): View[] {
  if (user.roles.includes('admin')) return ['trips', 'policy', 'audit']
  const views: View[] = []
  if (user.roles.includes('employee')) views.push('plan', 'trips')
  if (user.roles.includes('approver')) {
    views.push('approvals')
    if (!views.includes('trips')) views.push('trips')
  }
  views.push('policy')
  return views
}

/** 登录后默认落地视图。 */
function defaultView(user: UserIdentity): View {
  if (user.roles.includes('admin')) return 'trips'
  if (user.roles.includes('approver') && !user.roles.includes('employee')) return 'approvals'
  if (user.roles.includes('employee')) return 'plan'
  return 'policy'
}

/** 生成侧栏导航项（含审批待办数量）。 */
function navigationFor(
  user: UserIdentity,
  approvalCount?: number,
): { id: View; label: string; icon: IconName; count?: number }[] {
  const role = workspaceRole(user)
  const labels: Record<WorkspaceRole, Partial<Record<View, string>>> = {
    employee: { plan: '智能规划', trips: '我的差旅', approvals: '待我审批', policy: '差旅政策' },
    approver: { approvals: '待我审批', trips: '团队差旅', policy: '差旅政策' },
    admin: { trips: '任务总览', policy: '政策管理', audit: '审计与系统' },
  }
  const icons: Record<View, IconName> = { plan: 'spark', trips: 'briefcase', approvals: 'check', policy: 'book', audit: 'shield' }
  return allowedViews(user).map((id) => ({
    id,
    label: labels[role][id] ?? id,
    icon: icons[id],
    count: id === 'approvals' ? approvalCount : undefined,
  }))
}

/** 方案卡片展示模型：由 API TravelOption 映射而来。 */
interface DisplayOption {
  id: string
  tag: string
  tagTone: string
  mode: IconName
  number: string
  origin: string
  destination: string
  depart: string
  arrive: string
  duration: string
  seat: string
  hotel: string
  hotelMeta: string
  currencySymbol: string
  price: string
  transportPrice: string
  hotelPrice: string
  policy: string
  policyTone: string
  carbon: string
  facts: string[]
  live: true
}

/** 货币代码到展示符号。 */
function currencySymbol(currency: string): string {
  return { CNY: '¥', USD: '$', EUR: '€', GBP: '£' }[currency.toUpperCase()] ?? `${currency} `
}

function isoDatePart(value: unknown): string {
  if (typeof value !== 'string' || value.length < 10) return ''
  const day = value.slice(0, 10)
  return /^\d{4}-\d{2}-\d{2}$/.test(day) ? day : ''
}

/** 带货币符号的金额文案。 */
function amountText(amount: string | number, currency: string): string {
  const value = Number(amount)
  const formatted = Number.isFinite(value)
    ? new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 2 }).format(value)
    : String(amount)
  return `${currencySymbol(currency)}${formatted}`
}

/** 将时间戳格式化为本地时分展示。 */
function timeText(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value.slice(11, 16) || value
  return new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false }).format(date)
}

/** 差旅日期展示（墙钟日历日）。 */
function dateText(value: string): string {
  return formatTravelDate(value)
}

/** 差旅出发/抵达时刻展示（墙钟时分）。 */
function travelTimeText(value: string): string {
  return formatTravelTime(value)
}

/** 酒店通勤距离说明文案。 */
function hotelCommuteLabel(hotel: TravelOption['hotel']): string {
  if (!hotel) return ''
  if (hotel.commute_known === false || hotel.commute_minutes >= 1440) {
    return '通勤未知 · 未提供客户公司位置'
  }
  return `距客户约 ${hotel.commute_minutes} 分钟`
}

/** 将搜索失败原因转成用户可读的中文提示。 */
function searchOutcomeCopy(task: TripTask | null, fallback = ''): string {
  const failure = task?.failure ?? ''
  if (failure.includes('filtered_by_arrive_before') || failure.includes('arrived after the requested')) {
    return `供应商已经连上并返回了航班，但都不满足最晚抵达 ${intentText(task, 'arrive_by')}。Duffel 测试库存当天可能只有更晚的班次。请把最晚抵达改晚一些，或换一天再搜。`
  }
  if (failure.includes('filtered_by_depart_after') || failure.includes('departed before the requested')) {
    return `供应商已经连上并返回了航班，但都早于最早出发 ${intentText(task, 'departure_after')}。请放宽出发窗口后重试。`
  }
  return failure || fallback
}

/** 取任务原始自然语言指令（字段或首条用户消息）。 */
function originalInstruction(task: TripTask | null, fallback = ''): string {
  if (!task) return fallback
  if (task.original_instruction) return task.original_instruction
  const firstUser = (task.messages ?? []).find((item) => item.role === 'user')
  return firstUser?.content ?? fallback
}

/** 从意图字段推断住宿是否必需。 */
function lodgingRequirementFromTask(task: TripTask | null): LodgingRequirement {
  const value = task?.intent_fields.lodging_requirement
  if (value === 'REQUIRED' || value === 'NOT_REQUIRED') return value
  const hardConstraints = task?.intent_fields.hard_constraints
  if (
    (Array.isArray(hardConstraints) && hardConstraints.includes('hotel_required'))
    || task?.intent_fields.hotel_check_in
    || task?.intent_fields.hotel_check_out
  ) return 'REQUIRED'
  return 'UNSPECIFIED'
}

/** 住宿需求的中文标签。 */
function lodgingRequirementLabel(task: TripTask | null): string {
  const labels: Record<LodgingRequirement, string> = {
    REQUIRED: '需要酒店',
    NOT_REQUIRED: '无需酒店',
    UNSPECIFIED: '未说明',
  }
  return labels[lodgingRequirementFromTask(task)]
}

/** 将后端方案映射为方案卡片展示字段。 */
function displayOptionFromApi(
  option: TravelOption,
  index: number,
  lodgingRequirement: LodgingRequirement,
): DisplayOption {
  const total = Number(option.total_cost)
  const hotelTotal = Number(option.hotel?.total_price ?? 0)
  const transportTotal = Number.isFinite(total - hotelTotal) ? total - hotelTotal : option.total_cost
  const hours = Math.floor(option.total_duration_minutes / 60)
  const minutes = option.total_duration_minutes % 60
  const symbol = currencySymbol(option.currency)
  const isCompliant = option.policy_outcome === 'COMPLIANT'
  const needsApproval = option.policy_outcome === 'REQUIRES_APPROVAL'
  const reference = option.outbound.ref_id.length > 14
    ? `${option.outbound.ref_id.slice(0, 11)}…`
    : option.outbound.ref_id
  return {
    id: option.option_id,
    tag: index === 0 ? '综合推荐' : index === 1 ? '备选方案' : `方案 ${index + 1}`,
    tagTone: index === 0 ? 'best' : 'neutral',
    mode: option.outbound.mode === 'TRAIN' ? 'train' : 'plane',
    number: reference,
    origin: option.outbound.origin,
    destination: option.outbound.destination,
    depart: travelTimeText(option.outbound.depart_at),
    arrive: travelTimeText(option.outbound.arrive_at),
    duration: `${dateText(option.outbound.depart_at)}当地 · ${hours ? `${hours}时` : ''}${minutes}分 · ${option.outbound.is_direct ? '直达' : '中转'}`,
    seat: option.outbound.seat_class,
    hotel: option.hotel?.name ?? (lodgingRequirement === 'NOT_REQUIRED'
      ? '无需住宿'
      : lodgingRequirement === 'REQUIRED' ? '酒店搜索未返回结果' : '本方案未包含酒店'),
    hotelMeta: option.hotel
      ? `${option.hotel.nights}晚 · ${dateText(option.hotel.check_in)}–${dateText(option.hotel.check_out)} · ${hotelCommuteLabel(option.hotel)}`
      : lodgingRequirement === 'REQUIRED' ? '住宿为必需项，请重新规划或检查供应商记录' : '未计入酒店费用',
    currencySymbol: symbol,
    price: Number.isFinite(total) ? new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 2 }).format(total) : String(option.total_cost),
    transportPrice: amountText(transportTotal, option.currency),
    hotelPrice: option.hotel ? amountText(option.hotel.total_price, option.currency) : '—',
    policy: isCompliant ? '全部合规' : needsApproval ? '需要审批' : option.policy_outcome === 'FORBIDDEN' ? '政策禁止' : '证据不足',
    policyTone: isCompliant ? 'ok' : 'warn',
    carbon: `${option.outbound.provider} 实时库存`,
    facts: option.facts.length ? option.facts.slice(0, 3) : option.rule_evidence.slice(0, 3).map((rule) => rule.message),
    live: true,
  }
}

/** 读取意图字段并格式化为展示文本。 */
function intentText(task: TripTask | null, key: string, fallback = '待补充'): string {
  const value = task?.intent_fields[key]
  if (value === null || value === undefined || value === '') return fallback
  if (Array.isArray(value)) return value.join('、') || fallback
  return String(value)
}

/** 任务状态对应的徽章色调 class 后缀。 */
function stateBadgeTone(state: TaskState | null): string {
  if (!state) return 'gray'
  const tone = getStateMeta(state).tone
  if (tone === 'success') return 'green'
  if (tone === 'warning' || tone === 'waiting') return 'orange'
  if (tone === 'danger') return 'warn'
  if (tone === 'info') return 'blue'
  return 'gray'
}

/** 内联 SVG 图标组件。 */
function Icon({ name, size = 18 }: { name: IconName; size?: number }) {
  const paths: Record<IconName, React.ReactNode> = {
    spark: <><path d="M12 2l1.4 4.6L18 8l-4.6 1.4L12 14l-1.4-4.6L6 8l4.6-1.4L12 2Z"/><path d="M5 14l.8 2.2L8 17l-2.2.8L5 20l-.8-2.2L2 17l2.2-.8L5 14Z"/></>,
    briefcase: <><rect x="3" y="7" width="18" height="12" rx="2"/><path d="M8 7V5a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2M3 12h18M10 12v2h4v-2"/></>,
    check: <><path d="M9 11l2 2 4-4"/><path d="M12 3 4.5 6v5.2c0 4.2 3 8 7.5 9.8 4.5-1.8 7.5-5.6 7.5-9.8V6L12 3Z"/></>,
    book: <><path d="M4 5.5A2.5 2.5 0 0 1 6.5 3H11v16H6.5A2.5 2.5 0 0 0 4 21.5v-16ZM20 5.5A2.5 2.5 0 0 0 17.5 3H13v16h4.5a2.5 2.5 0 0 1 2.5 2.5v-16Z"/></>,
    shield: <><path d="M12 3 4.5 6v5.2c0 4.2 3 8 7.5 9.8 4.5-1.8 7.5-5.6 7.5-9.8V6L12 3Z"/><path d="m9 12 2 2 4-4"/></>,
    plus: <path d="M12 5v14M5 12h14"/>,
    bell: <><path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9ZM10 21h4"/></>,
    arrow: <><path d="M5 12h14M14 7l5 5-5 5"/></>,
    train: <><rect x="5" y="3" width="14" height="15" rx="4"/><path d="M8 7h8M7 12h10M8 21l3-3M16 21l-3-3M9 15h.01M15 15h.01"/></>,
    plane: <path d="m3 11 18-7-6 16-3.5-6L3 11Zm8.5 3L21 4"/>,
    hotel: <><path d="M4 21V4h11v17M15 10h5v11M8 8h3M8 12h3M8 16h3M2 21h20"/></>,
    clock: <><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></>,
    leaf: <><path d="M19 4C10 4 5 8.5 5 14c0 3 2 5 5 5 5.5 0 9-6 9-15Z"/><path d="M5 21c2-5 5-8 10-11"/></>,
    info: <><circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7h.01"/></>,
    chevron: <path d="m9 18 6-6-6-6"/>,
    close: <><path d="m6 6 12 12M18 6 6 18"/></>,
    search: <><circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/></>,
    filter: <path d="M4 6h16M7 12h10M10 18h4"/>,
    download: <><path d="M12 3v12M7 10l5 5 5-5M5 21h14"/></>,
    user: <><circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/></>,
    link: <><path d="M10 13a5 5 0 0 0 7.5.5l2-2a5 5 0 0 0-7-7l-1.1 1.1"/><path d="M14 11a5 5 0 0 0-7.5-.5l-2 2a5 5 0 0 0 7 7l1.1-1.1"/></>,
  }
  return <svg className="icon" width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden="true">{paths[name]}</svg>
}

/** 状态/标签徽章。 */
function Badge({ children, tone = 'gray' }: { children: React.ReactNode; tone?: string }) {
  return <span className={`badge badge-${tone}`}>{children}</span>
}

/** 左侧导航：按角色渲染菜单、服务状态与账号菜单。 */
function Sidebar({ activeView, onChange, user, onLogout }: {
  activeView: View
  onChange: (view: View) => void
  user: UserIdentity
  onLogout: (() => void) | null
}) {
  const [accountOpen, setAccountOpen] = useState(false)
  const [approvalCount, setApprovalCount] = useState<number | undefined>()
  const { health, authEnabled, switchDevelopmentRole } = useAuth()
  useEffect(() => {
    if (!user.roles.includes('approver')) {
      setApprovalCount(undefined)
      return
    }
    let cancelled = false
    api.approvalInbox(100).then((items) => {
      if (!cancelled) setApprovalCount(items.length)
    }).catch(() => {
      if (!cancelled) setApprovalCount(undefined)
    })
    return () => { cancelled = true }
  }, [user, activeView])
  const visibleNav = navigationFor(user, approvalCount)
  const roleLabel = user.roles.includes('admin') ? '系统管理员' : user.roles.includes('approver') ? '直属审批人' : '员工'
  const initials = user.user_id.slice(0, 2).toUpperCase()
  const modelReady = health?.language_model_ready !== false
  const modelLabel = health?.language_model_status === 'billing_blocked'
    ? '模型额度不足'
    : health?.language_model_status === 'auth_failed'
      ? '模型认证失败'
      : '服务正常'
  return (
    <aside className="sidebar">
      <button className="brand" onClick={() => onChange(defaultView(user))} aria-label="澄行差旅首页">
        <span className="brand-mark"><span /><span /><span /></span>
        <span><b>澄行</b><small>企业差旅</small></span>
      </button>
      <nav aria-label="主导航">
        {visibleNav.map((item) => (
          <button key={item.id} className={activeView === item.id ? 'active' : ''} onClick={() => onChange(item.id)}>
            <Icon name={item.icon} />
            <span>{item.label}</span>
            {item.count && <em>{item.count}</em>}
          </button>
        ))}
      </nav>
      <div className="sidebar-foot">
        <div className={`service-status ${modelReady ? '' : 'degraded'}`.trim()}><i />{modelLabel} <span>V1.0</span></div>
        <button className="profile-mini" aria-expanded={accountOpen} onClick={() => setAccountOpen((open) => !open)}>
          <span className="avatar">{initials}</span>
          <span><b>{user.user_id}</b><small>{roleLabel}</small></span>
          <span className="more">•••</span>
        </button>
        {accountOpen && <div className="profile-menu">
          <span>当前身份<small>{user.employee_id ?? user.user_id}</small></span>
          {!authEnabled && <>
            <span className="development-role-label">本地开发身份</span>
            {([
              ['employee', '员工 E1001'],
              ['approver', '审批人 M2001'],
              ['admin', '管理员 A9001'],
            ] as const).map(([role, label]) => (
              <button
                key={role}
                type="button"
                className={user.roles.includes(role) ? 'active' : ''}
                onClick={() => {
                  switchDevelopmentRole(role)
                  setAccountOpen(false)
                  onChange(role === 'employee' ? 'plan' : role === 'approver' ? 'approvals' : 'trips')
                }}
              >
                {label}
              </button>
            ))}
          </>}
          {onLogout && <button onClick={onLogout}>退出登录</button>}
        </div>}
      </div>
    </aside>
  )
}

/** 认证尚未就绪时的全屏加载态。 */
function LoadingScreen() {
  return <main className="auth-screen"><div className="auth-loading"><span className="brand-mark"><span /><span /><span /></span><b>正在连接澄行服务</b><i /></div></main>
}

/** 登录页：认证开启时提交凭证，否则提示重连 API。 */
function LoginScreen() {
  const { authEnabled, error, login, refreshHealth } = useAuth()
  const [userId, setUserId] = useState('')
  const [password, setPassword] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [formError, setFormError] = useState('')

  const handleSubmit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!userId.trim() || !password) {
      setFormError('请输入用户 ID 和密码')
      return
    }
    setSubmitting(true)
    setFormError('')
    try {
      await login(userId.trim(), password)
    } catch (loginError) {
      setFormError(loginError instanceof Error ? loginError.message : '登录失败')
    } finally {
      setSubmitting(false)
    }
  }

  return <main className="auth-screen">
    <section className="login-panel" aria-labelledby="login-title">
      <div className="login-brand"><span className="brand-mark"><span /><span /><span /></span><span><b>澄行</b><small>企业差旅</small></span></div>
      <div className="eyebrow">SECURE TRAVEL DESK</div>
      <h1 id="login-title">登录差旅工作台</h1>
      <p>使用企业分配的用户 ID 继续。你的角色将决定可查看的行程与审批范围。</p>
      {authEnabled ? <form onSubmit={handleSubmit}>
        <label><span>用户 ID</span><input autoComplete="username" value={userId} onChange={(event) => setUserId(event.target.value)} placeholder="例如 E1001" autoFocus /></label>
        <label><span>密码</span><input type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} placeholder="输入登录密码" /></label>
        {(formError || error) && <div className="auth-error" role="alert"><Icon name="info" size={16} />{formError || error}</div>}
        <button className="primary full" type="submit" data-testid="login-submit" disabled={submitting}>{submitting ? '正在验证…' : '安全登录'}{!submitting && <Icon name="arrow" />}</button>
      </form> : <div className="auth-unavailable">
        <Icon name="info" />
        <div><b>暂时无法连接 API 服务</b><p>{error || '正在确认后端认证配置。'}</p></div>
        <button className="outline-button" onClick={() => void refreshHealth()}>重新连接</button>
      </div>}
      <div className="login-boundary"><Icon name="shield" size={16} />短期签名会话 · 资源级权限 · 禁止自动预订与支付</div>
    </section>
    <aside className="login-aside"><div><span>TRAVEL / EVIDENCE / CONTROL</span><h2>每一次差旅决定，<br />都有清晰依据。</h2><p>路线、政策、库存与审批证据被固定在同一条可审计链路中。</p></div><div className="login-proof"><span><i />政策引擎在线</span><span>CN-TRAVEL v3.2</span></div></aside>
  </main>
}

/** 顶栏：当前视图标题、通知与新建差旅入口。 */
function Header({ activeView, onNewTrip, user, onLogout }: {
  activeView: View
  onNewTrip: (() => void) | null
  user: UserIdentity
  onLogout: (() => void) | null
}) {
  const role = workspaceRole(user)
  const titles: Record<View, string> = {
    plan: '智能规划',
    trips: role === 'admin' ? '任务总览' : role === 'approver' ? '团队差旅' : '我的差旅',
    approvals: '审批中心',
    policy: role === 'admin' ? '政策管理' : '差旅政策',
    audit: '审计与系统',
  }
  return (
    <header className="topbar">
      <div><span className="mobile-brand">澄行</span><b>{titles[activeView]}</b></div>
      <div className="top-actions">
        <button className="icon-button" aria-label="通知"><Icon name="bell" /><i /></button>
        {onLogout && <button className="header-account" onClick={onLogout} title="退出登录"><span className="avatar">{user.user_id.slice(0, 2).toUpperCase()}</span><small>退出</small></button>}
        {onNewTrip && <span className="divider" />}
        {onNewTrip && <button className="primary small" onClick={onNewTrip}><Icon name="plus" />新建差旅</button>}
      </div>
    </header>
  )
}

/** 智能规划页的自然语言指令输入区与对话摘要。 */
function InlineAgentComposer({ task, busy, error, message, onMessageChange, onSubmit }: {
  task: TripTask | null
  busy: boolean
  error: string
  message: string
  onMessageChange: (value: string) => void
  onSubmit: (message: string) => Promise<boolean>
}) {
  const suggestions = ['14:00 前抵达', '优先高铁', '酒店靠近客户', '避免早班']
  const stateMeta = task ? getStateMeta(task.state) : null
  const userTurns = (task?.messages ?? []).filter((item) => item.role === 'user')
  const assistantMessage = error
    ? '刚才的请求没有完成。请检查提示后重试，已输入的内容不会丢失。'
    : busy
      ? '正在理解你的指令，并调用政策与库存服务生成结果……'
      : task
          ? stateMeta?.description ?? '任务已更新。'
          : '你好，需要我规划哪段行程？请直接补充目的地、时间、交通、酒店或预算要求。'

  const appendSuggestion = (suggestion: string) => {
    if (message.includes(suggestion)) return
    const separator = message.trim() && !/[。！？]$/.test(message.trim()) ? '，' : ''
    onMessageChange(`${message.trim()}${separator}${suggestion}。`)
  }

  const submit = async () => {
    if (!message.trim() || busy) return
    await onSubmit(message.trim())
  }

  return (
    <section className="agent-command-center" aria-labelledby="agent-command-title">
      <div className="agent-command-rail">
        <div className="eyebrow"><Icon name="spark" />TRAVEL AGENT</div>
        <h2 id="agent-command-title">直接告诉我<br />你的差旅安排</h2>
        <p>一句话描述目的地、日期和偏好，我会补齐信息并校验政策。</p>
        <span className="agent-live"><i />{busy ? 'Agent 工作中' : task ? `任务 ${task.task_id.slice(0, 8)}` : 'API 已就绪'}</span>
      </div>
      <div className="agent-conversation">
        <div className="assistant-turn">
          <span className="assistant-avatar">澄</span>
          <div><b>澄行差旅助理 {stateMeta && <em>· {stateMeta.label}</em>}</b><p>{assistantMessage}</p></div>
        </div>
        {userTurns.map((turn, index) => (
          <div className="user-turn" key={`${turn.created_at}-${index}`}>
            <span className="user-avatar">我</span>
            <div><b>{index === 0 ? '你的原始指令' : `补充说明 ${index}`}</b><p>{turn.content}</p></div>
          </div>
        ))}
        {error && <div className="agent-api-error" role="alert"><Icon name="info" size={15} />{error}</div>}
        <div className="inline-composer-input">
          <label htmlFor="inline-trip-request">你的指令</label>
          <button type="button" onClick={() => onMessageChange('')}>清空</button>
          <textarea
            id="inline-trip-request"
            name="inline-trip-request"
            value={message}
            onChange={(event) => onMessageChange(event.target.value)}
            onKeyDown={(event) => {
              if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') void submit()
            }}
            placeholder="例如：下周二从北京去上海见客户，优先高铁，酒店离客户近一点……"
          />
        </div>
        <div className="agent-command-actions">
          <div className="command-suggestions" aria-label="常用指令">
            {!message.trim() && <button type="button" onClick={() => onMessageChange(EXAMPLE_TRIP_MESSAGE)}>填入示例</button>}
            {suggestions.map((suggestion) => <button type="button" key={suggestion} onClick={() => appendSuggestion(suggestion)}>+ {suggestion}</button>)}
          </div>
          <span className="command-shortcut">⌘ Enter 发送</span>
          <button className="primary command-submit" type="button" disabled={!message.trim() || busy} onClick={() => void submit()}>{busy ? '正在连接 API…' : task && (task.state === 'NEEDS_CLARIFICATION' || task.state === 'NEEDS_STRUCTURED_INPUT') ? '补充并继续' : '发送并规划'} {!busy && <Icon name="arrow" />}</button>
        </div>
        <div className="agent-boundary"><Icon name="shield" size={14} />AI 负责理解与规划，政策结论由规则引擎校验；不会自动预订或支付。</div>
      </div>
    </section>
  )
}

/** 澄清字段 key → 中文标签。 */
const clarificationFieldLabels: Record<string, string> = {
  origin: '出发城市',
  destination: '目的城市',
  departure_after: '最早出发',
  arrive_by: '最晚抵达',
  return_after: '返程出发',
  return_before: '返程抵达',
  hotel_check_in: '入住日期',
  hotel_check_out: '退房日期',
  return_trip: '返程安排',
  hotel_need: '酒店需求',
  transport_mode: '交通方式',
  hard_constraints: '交通方式',
  client_location: '客户公司位置',
}

/** 搜索必填的核心意图字段。 */
const coreSearchFields = ['origin', 'destination', 'departure_after', 'arrive_by'] as const

/** 判断意图字段是否为空（含空数组）。 */
function isEmptyIntentValue(value: unknown): boolean {
  return value === null || value === undefined || value === '' || (Array.isArray(value) && value.length === 0)
}

/** 综合 API 缺失字段与意图空槽，得到待补充字段列表。已填槽不再显示为缺失。 */
function derivedMissingFields(task: TripTask): string[] {
  const fromApi = [
    ...task.missing_required_fields,
    ...task.uncertain_slots,
    ...validClarificationQuestions(task).flatMap((question) => question.slots),
  ]
  const fromIntent: string[] = coreSearchFields.filter((key) => isEmptyIntentValue(task.intent_fields[key]))
  if (lodgingRequirementFromTask(task) === 'REQUIRED') {
    if (isEmptyIntentValue(task.intent_fields.hotel_check_in)) fromIntent.push('hotel_check_in')
    if (isEmptyIntentValue(task.intent_fields.hotel_check_out)) fromIntent.push('hotel_check_out')
  }
  const hasReturn = !isEmptyIntentValue(task.intent_fields.return_after)
    || !isEmptyIntentValue(task.intent_fields.return_before)
  if (hasReturn) {
    if (isEmptyIntentValue(task.intent_fields.return_after)) fromIntent.push('return_after')
    if (isEmptyIntentValue(task.intent_fields.return_before)) fromIntent.push('return_before')
  }
  return Array.from(new Set([...fromApi, ...fromIntent])).filter((key) => isEmptyIntentValue(task.intent_fields[key]))
}

/** 已识别意图字段的中文标签与值对。 */
function knownIntentEntries(task: TripTask): [string, string][] {
  return [
    'origin',
    'destination',
    'departure_after',
    'arrive_by',
    'return_after',
    'return_before',
    'hotel_check_in',
    'hotel_check_out',
    'client_location',
  ]
    .filter((key) => !isEmptyIntentValue(task.intent_fields[key]))
    .map((key) => [clarificationFieldLabels[key] ?? key, intentText(task, key)])
}

/** 过滤出结构合法的澄清问题列表。 */
function validClarificationQuestions(task: TripTask): ClarificationQuestion[] {
  if (!Array.isArray(task.clarification_questions)) return []
  return task.clarification_questions.filter((item): item is ClarificationQuestion => (
    typeof item === 'object'
    && item !== null
    && typeof item.id === 'string'
    && typeof item.header === 'string'
    && typeof item.question === 'string'
    && Array.isArray(item.options)
  ))
}

/** 澄清面板：展示缺失槽位、快捷选项与自定义答案提交。 */
const TIME_OPTIONS: string[] = Array.from({ length: 24 * 4 }, (_, index) => {
  const hour = Math.floor(index / 4)
  const minute = (index % 4) * 15
  return `${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`
})

function nextTimeAfter(value: string): string {
  const index = TIME_OPTIONS.indexOf(value)
  if (index < 0) return TIME_OPTIONS[1]
  return TIME_OPTIONS[Math.min(index + 1, TIME_OPTIONS.length - 1)]
}

function TimeRangePicker({
  kind,
  busy,
  defaultDate,
  onSubmit,
}: {
  kind: 'window' | 'return_window' | 'date'
  busy: boolean
  defaultDate?: string
  onSubmit: (value: string) => void
}) {
  const today = new Date()
  const isoDate = defaultDate && /^\d{4}-\d{2}-\d{2}$/.test(defaultDate)
    ? defaultDate
    : `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, '0')}-${String(today.getDate()).padStart(2, '0')}`
  const [date, setDate] = useState(isoDate)
  const [start, setStart] = useState('08:00')
  const [end, setEnd] = useState('18:00')
  const arrivalOptions = TIME_OPTIONS.filter((item) => item > start)
  const canSubmit = Boolean(date) && (kind === 'date' || (Boolean(start) && Boolean(end) && end > start))
  return (
    <div className="clarification-time-range" aria-label="选择时间区间">
      <label>
        日期
        <input type="date" value={date} disabled={busy} onChange={(event) => setDate(event.target.value)} />
      </label>
      {kind !== 'date' && <>
        <label>
          开始时间
          <select
            value={start}
            disabled={busy}
            onChange={(event) => {
              const nextStart = event.target.value
              setStart(nextStart)
              if (end <= nextStart) setEnd(nextTimeAfter(nextStart))
            }}
          >
            {TIME_OPTIONS.filter((item) => item < '23:45').map((item) => (
              <option key={item} value={item}>{item}</option>
            ))}
          </select>
        </label>
        <label>
          到达时间
          <select value={end} disabled={busy || arrivalOptions.length === 0} onChange={(event) => setEnd(event.target.value)}>
            {arrivalOptions.map((item) => (
              <option key={item} value={item}>{item}</option>
            ))}
          </select>
        </label>
      </>}
      <button
        type="button"
        className="primary"
        disabled={busy || !canSubmit}
        onClick={() => {
          if (kind === 'date') {
            onSubmit(`date:${date}`)
            return
          }
          onSubmit(`${kind}:${date}T${start}/${date}T${end}`)
        }}
      >
        确认区间
      </button>
    </div>
  )
}

function ClarificationPanel({ task, busy, error, onSubmit }: {
  task: TripTask
  busy: boolean
  error: string
  onSubmit: (message: string) => Promise<boolean>
}) {
  const questions = useMemo(() => validClarificationQuestions(task).filter((question) => {
    if (!question.slots.length) return true
    return question.slots.some((slot) => isEmptyIntentValue(task.intent_fields[slot]))
  }), [task])
  const [customAnswers, setCustomAnswers] = useState<Record<string, string>>(
    () => readClarificationDraft(task.task_id)?.customAnswers ?? {},
  )
  const [pendingAnswer, setPendingAnswer] = useState('')
  const [lastFailedAnswer, setLastFailedAnswer] = useState(
    () => readClarificationDraft(task.task_id)?.lastFailedAnswer ?? '',
  )
  const [waitSeconds, setWaitSeconds] = useState(0)
  const laterUserTurns = (task.messages ?? []).filter((item) => item.role === 'user').slice(1)
  const missingFields = derivedMissingFields(task)
  const missingLabels = missingFields.map((field) => clarificationFieldLabels[field] ?? field)
  const knownFields = knownIntentEntries(task)
  const instruction = originalInstruction(task)
  const retry = clarificationRetry({
    originalInstruction: instruction,
    lastFailedAnswer,
    hasSubmitError: Boolean(error),
  })
  const fallbackQuestion = task.clarification_question
    || (missingLabels.length > 0
      ? `请补充：${missingLabels.join('、')}。`
      : '请补充出差关键信息后继续。')
  const extractFailure = task.extract_failure?.class
  const isBillingFailure = extractFailure === 'billing'
  const isAuthFailure = extractFailure === 'auth'

  useEffect(() => {
    const restored = readClarificationDraft(task.task_id)
    setCustomAnswers(restored?.customAnswers ?? {})
    setLastFailedAnswer(restored?.lastFailedAnswer ?? '')
    setPendingAnswer('')
  }, [task.task_id, task.clarification_rounds])

  useEffect(() => {
    if (!busy) {
      setWaitSeconds(0)
      return
    }
    const started = Date.now()
    const timer = window.setInterval(() => {
      setWaitSeconds(Math.max(1, Math.round((Date.now() - started) / 1000)))
    }, 250)
    return () => window.clearInterval(timer)
  }, [busy])

  const persistDraft = (answers: Record<string, string>, failedAnswer: string) => {
    writeClarificationDraft(task.task_id, {
      customAnswers: answers,
      lastFailedAnswer: failedAnswer,
    })
  }

  const submitAnswer = async (answer: string) => {
    const normalized = answer.trim()
    if (!normalized || busy) return
    setPendingAnswer(normalized)
    persistDraft(customAnswers, normalized)
    const succeeded = await onSubmit(normalized)
    if (succeeded) {
      clearClarificationDraft(task.task_id)
      setLastFailedAnswer('')
      return
    }
    setLastFailedAnswer(normalized)
    persistDraft(customAnswers, normalized)
    setPendingAnswer('')
  }

  const submitCustomAnswer = async (questionId: string) => {
    const answer = customAnswers[questionId]?.trim() ?? ''
    if (!answer) return
    await submitAnswer(answer)
  }

  return (
    <section className="clarification-panel" aria-labelledby="clarification-title">
      <header className="clarification-header">
        <div className="clarification-signal"><span>{String(Math.max(questions.length, 1)).padStart(2, '0')}</span><i /></div>
        <div>
          <div className="section-kicker">{isBillingFailure ? '模型额度不足' : isAuthFailure ? '模型认证失败' : '需要你补充'}</div>
          <h2 id="clarification-title">{isBillingFailure || isAuthFailure ? '不是你漏填，是模型这次没抽成' : '把缺失信息补齐，我们就能继续'}</h2>
          <p>{busy
            ? `正在根据你刚提交的补充重新抽取行程，已等待 ${waitSeconds} 秒。点选模板通常几秒内就能继续，自由文本可能需要更久。`
            : isBillingFailure
              ? '原指令已保存。请检查账户额度后用原指令重试，或补全下面仍为空的字段。'
              : task.failure
                ? '模型这次没能把你的指令写成行程字段。下面是当前仍为空的项。'
                : '选择最符合的答案会立即提交；也可以在对应问题下直接填写。'}</p>
        </div>
        <Badge tone={busy ? 'blue' : 'warn'}>{busy ? '正在处理' : '等待回答'}</Badge>
      </header>

      {busy && <div className="clarification-progress" role="status">
        <span className="progress-orbit spinning"><Icon name="spark" /></span>
        <div>
          <b>补充已提交，正在继续规划</b>
          <p>已等待 {waitSeconds} 秒。请不要关闭页面；完成后会直接更新这张卡片。</p>
        </div>
      </div>}

      {instruction && <div className="clarification-instruction">
        <span>原始指令</span>
        <blockquote>“{instruction}”</blockquote>
        {laterUserTurns.length > 0 && <ol className="clarification-followups">
          {laterUserTurns.map((turn, index) => (
            <li key={`${turn.created_at}-${index}`}>已补充：{turn.content}</li>
          ))}
        </ol>}
        <button type="button" className="retry-original" disabled={busy || !retry.text.trim()} onClick={() => void submitAnswer(retry.text)}>
          {busy ? '正在重试…' : retry.label}
        </button>
        {retry.mode === 'failed_supplement' && <small className="retry-hint">将重试你刚才的补充，不会改回示例行程。</small>}
      </div>}

      <div className="clarification-missing" aria-label="待补充信息">
        <span>当前缺失</span>
        {missingLabels.length > 0
          ? missingLabels.map((label) => <b key={label}>{label}</b>)
          : <em>没有列出具体字段，请按问题补充</em>}
      </div>

      {knownFields.length > 0 && <div className="clarification-known" aria-label="已识别信息">
        <span>已识别</span>
        {knownFields.map(([label, value]) => <b key={label}>{label} · {value}</b>)}
      </div>}

      {error && <div className="clarification-error" role="alert"><Icon name="info" size={15} />{error}</div>}

      <div className="clarification-question-list">
        {(questions.length > 0 ? questions : [{
          id: 'details',
          header: '行程信息',
          question: fallbackQuestion,
          options: [],
          multi_select: false,
          slots: missingFields,
        }]).map((question, questionIndex) => {
          const quickOptions = question.options.filter((option) => option.value !== 'free_text')
          const customAnswer = customAnswers[question.id] ?? ''
          return (
            <article className="clarification-question" key={question.id}>
              <div className="clarification-question-number">{String(questionIndex + 1).padStart(2, '0')}</div>
              <div className="clarification-question-body">
                <span className="clarification-question-header">{question.header}</span>
                <h3>{question.question}</h3>
                {(question.input_kind === 'time_range' || question.input_kind === 'date') && (
                  <TimeRangePicker
                    kind={question.input_kind === 'date' ? 'date' : (question.id === 'return_times' ? 'return_window' : 'window')}
                    busy={busy}
                    defaultDate={
                      isoDatePart(task.intent_fields.return_after)
                      || isoDatePart(task.intent_fields.departure_after)
                      || isoDatePart(task.intent_fields.arrive_by)
                    }
                    onSubmit={(value) => void submitAnswer(value)}
                  />
                )}
                {quickOptions.length > 0 && <div className={`clarification-options${question.id === 'cities' ? ' city-options' : ''}`} aria-label={`${question.header}选项`}>
                  {quickOptions.map((option) => {
                    const isPending = pendingAnswer === option.value
                    return <button
                      type="button"
                      key={option.value}
                      data-testid={`clarification-option-${option.value}`}
                      className={isPending ? 'pending' : ''}
                      disabled={busy}
                      onClick={() => void submitAnswer(option.value)}
                    >
                      <span><b>{option.label}</b><small>{option.description}</small></span>
                      <em>{isPending && busy ? '提交中' : '选择'} <Icon name="arrow" size={14} /></em>
                    </button>
                  })}
                </div>}
                {quickOptions.length < 2 && !question.input_kind && <div className="clarification-custom-answer">
                  <label htmlFor={`clarification-${question.id}`}>{question.id === 'cities' ? '其他城市' : '自定义答案'}</label>
                  <div>
                    <input
                      id={`clarification-${question.id}`}
                      value={customAnswer}
                      disabled={busy}
                      onChange={(event) => {
                        const next = { ...customAnswers, [question.id]: event.target.value }
                        setCustomAnswers(next)
                        persistDraft(next, lastFailedAnswer)
                      }}
                      onKeyDown={(event) => {
                        if (event.key === 'Enter') {
                          event.preventDefault()
                          void submitCustomAnswer(question.id)
                        }
                      }}
                      placeholder="直接填写具体信息"
                    />
                    <button type="button" disabled={!customAnswer.trim() || busy} onClick={() => void submitCustomAnswer(question.id)}>
                      {pendingAnswer === customAnswer.trim() && busy ? '提交中…' : '提交'}
                    </button>
                  </div>
                </div>}
              </div>
            </article>
          )
        })}
      </div>
      <footer><Icon name="shield" size={14} />每次回答都会交给真实 API 重新校验，系统不会自动预订或支付。</footer>
    </section>
  )
}

/** 将 ISO 时间转为 `datetime-local` 输入值。 */
function toDatetimeLocal(value: unknown): string {
  if (typeof value !== 'string' || !value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value.length >= 16 ? value.slice(0, 16) : ''
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`
}

/** 将日期字符串截成 `date` 输入所需的 YYYY-MM-DD。 */
function toDateInput(value: unknown): string {
  if (typeof value !== 'string' || !value) return ''
  return value.slice(0, 10)
}

/** 将 `datetime-local` 值补成带 +08:00 的 ISO 字符串。 */
function datetimeLocalToIso(value: string): string {
  if (!value) return ''
  return value.length === 16 ? `${value}:00+08:00` : value
}

/** 澄清轮次用尽后的结构化行程表单。 */
function StructuredRequestForm({ task, busy, error, onSubmit }: {
  task: TripTask
  busy: boolean
  error: string
  onSubmit: (payload: StructuredTripCreate) => Promise<boolean>
}) {
  const { user } = useAuth()
  const [origin, setOrigin] = useState(() => String(task.intent_fields.origin ?? ''))
  const [destination, setDestination] = useState(() => String(task.intent_fields.destination ?? ''))
  const [departureAfter, setDepartureAfter] = useState(() => toDatetimeLocal(task.intent_fields.departure_after))
  const [arriveBy, setArriveBy] = useState(() => toDatetimeLocal(task.intent_fields.arrive_by))
  const [returnAfter, setReturnAfter] = useState(() => toDatetimeLocal(task.intent_fields.return_after))
  const [returnBefore, setReturnBefore] = useState(() => toDatetimeLocal(task.intent_fields.return_before))
  const [hotelCheckIn, setHotelCheckIn] = useState(() => toDateInput(task.intent_fields.hotel_check_in))
  const [hotelCheckOut, setHotelCheckOut] = useState(() => toDateInput(task.intent_fields.hotel_check_out))

  useEffect(() => {
    const fields = task.intent_fields
    setOrigin(String(fields.origin ?? ''))
    setDestination(String(fields.destination ?? ''))
    setDepartureAfter(toDatetimeLocal(fields.departure_after))
    setArriveBy(toDatetimeLocal(fields.arrive_by))
    setReturnAfter(toDatetimeLocal(fields.return_after))
    setReturnBefore(toDatetimeLocal(fields.return_before))
    setHotelCheckIn(toDateInput(fields.hotel_check_in))
    setHotelCheckOut(toDateInput(fields.hotel_check_out))
  }, [task])

  const canSubmit = Boolean(
    origin.trim()
    && destination.trim()
    && departureAfter
    && arriveBy
    && !busy,
  )

  const submit = async () => {
    if (!canSubmit || !user) return
    await onSubmit({
      traveler_id: user.employee_id ?? user.user_id,
      origin: origin.trim(),
      destination: destination.trim(),
      departure_after: datetimeLocalToIso(departureAfter),
      arrive_by: datetimeLocalToIso(arriveBy),
      return_after: returnAfter ? datetimeLocalToIso(returnAfter) : null,
      return_before: returnBefore ? datetimeLocalToIso(returnBefore) : null,
      hotel_check_in: hotelCheckIn || null,
      hotel_check_out: hotelCheckOut || null,
    })
  }

  return (
    <section className="clarification-panel structured-form" aria-labelledby="structured-form-title" data-testid="structured-request-form">
      <header className="clarification-header">
        <div className="clarification-signal"><span>表</span><i /></div>
        <div>
          <div className="section-kicker">澄清次数已用尽</div>
          <h2 id="structured-form-title">用结构化表单提交后才能搜索</h2>
          <p>{task.failure || '请补齐出发地、目的地和出行时间。系统不会从空槽编造城市。'}</p>
        </div>
        <Badge tone="warn">结构化输入</Badge>
      </header>
      {error && <div className="clarification-error" role="alert"><Icon name="info" size={15} />{error}</div>}
      <div className="structured-grid">
        <label>出发城市<input data-testid="structured-origin" value={origin} onChange={(event) => setOrigin(event.target.value)} /></label>
        <label>目的城市<input data-testid="structured-destination" value={destination} onChange={(event) => setDestination(event.target.value)} /></label>
        <label>最早出发<input data-testid="structured-departure" type="datetime-local" value={departureAfter} onChange={(event) => setDepartureAfter(event.target.value)} /></label>
        <label>最晚抵达<input data-testid="structured-arrive" type="datetime-local" value={arriveBy} onChange={(event) => setArriveBy(event.target.value)} /></label>
        <label>返程出发<input type="datetime-local" value={returnAfter} onChange={(event) => setReturnAfter(event.target.value)} /></label>
        <label>返程抵达<input type="datetime-local" value={returnBefore} onChange={(event) => setReturnBefore(event.target.value)} /></label>
        <label>入住日期<input type="date" value={hotelCheckIn} onChange={(event) => setHotelCheckIn(event.target.value)} /></label>
        <label>退房日期<input type="date" value={hotelCheckOut} onChange={(event) => setHotelCheckOut(event.target.value)} /></label>
      </div>
      <div className="structured-actions">
        <button className="primary" type="button" data-testid="structured-submit" disabled={!canSubmit} onClick={() => void submit()}>
          {busy ? '正在提交并搜索…' : '提交表单并搜索'}
        </button>
      </div>
    </section>
  )
}

/** 单个候选方案卡片（选择 / 对比）。 */
function OptionCard({ option, selected, compared, onSelect, onCompare }: {
  option: DisplayOption
  selected: boolean
  compared: boolean
  onSelect: () => void
  onCompare: () => void
}) {
  return (
    <article className={`option-card ${selected ? 'selected' : ''}`} data-testid={`option-card-${option.id}`}>
      <div className="option-topline">
        <div><Badge tone={option.tagTone}>{option.tag}</Badge><Badge tone={option.policyTone}>{option.policy === '全部合规' && <span className="mini-check">✓</span>}{option.policy}</Badge></div>
        <label className="compare-check"><input type="checkbox" checked={compared} onChange={onCompare} />加入对比</label>
      </div>
      <div className="option-body">
        <div className="itinerary">
          <div className="route-main">
            <div className={`transport-icon ${option.mode}`}><Icon name={option.mode} size={20} /></div>
            <div className="time-place"><strong>{option.depart}</strong><span>{option.origin}</span></div>
            <div className="route-line"><span>{option.number} · {option.seat}</span><i /><small>{option.duration}</small></div>
            <div className="time-place right"><strong>{option.arrive}</strong><span>{option.destination}</span></div>
          </div>
          <div className="hotel-row"><span className="hotel-icon"><Icon name="hotel" size={17} /></span><div><b>{option.hotel}</b><small>{option.hotelMeta}</small></div></div>
        </div>
        <div className="option-price">
          <span>预估总价</span><strong><small>{option.currencySymbol}</small>{option.price}</strong><em>含税费</em>
          <button className={selected ? 'selected-button' : 'outline-button'} data-testid={`select-option-${option.id}`} onClick={onSelect}>{selected ? '已选择' : '选择方案'}</button>
        </div>
      </div>
      <div className="option-foot">
        <span><Icon name={option.live ? 'link' : 'leaf'} size={15} />{option.carbon}</span>
        {option.facts.map((fact) => <span key={fact}><i />{fact}</span>)}
        <button aria-label="展开详情"><Icon name="chevron" size={16} /></button>
      </div>
    </article>
  )
}

/**
 * 智能规划主视图：驱动任务状态机对应的 UI 分支。
 * NEEDS_STRUCTURED_INPUT → 表单；NEEDS_CLARIFICATION → 澄清；否则自然语言作曲器；有方案后展示选择区。
 */
function PlanView({ onToast, composerEpoch }: { onToast: (text: string) => void; composerEpoch: number }) {
  const { user } = useAuth()
  const [task, setTask] = useState<TripTask | null>(null)
  const [selectedId, setSelectedId] = useState('')
  const [compared, setCompared] = useState<string[]>([])
  const [tab, setTab] = useState<'options' | 'request' | 'timeline'>('options')
  const [instruction, setInstruction] = useState('')
  const [draftMessage, setDraftMessage] = useState(() => initialComposerMessage())
  const [composingNew, setComposingNew] = useState(false)
  const [apiBusy, setApiBusy] = useState(false)
  const [actionBusy, setActionBusy] = useState(false)
  const [apiError, setApiError] = useState('')
  const [businessReason, setBusinessReason] = useState('')
  const composingNewRef = useRef(false)

  const updateDraft = (value: string) => {
    setDraftMessage(value)
    writeComposerDraft(value)
  }

  useEffect(() => {
    if (composerEpoch === 0) return
    composingNewRef.current = true
    setTask(null)
    setSelectedId('')
    setCompared([])
    setInstruction('')
    setApiError('')
    setComposingNew(true)
    setDraftMessage('')
    clearComposerDraft()
  }, [composerEpoch])

  useEffect(() => {
    let cancelled = false
    api.listTasks({ summary: false, limit: 1 }).then((items) => {
      if (cancelled || composingNewRef.current) return
      const latest = items.find((item): item is TripTask => item.summary === false)
      if (latest) {
        setTask(latest)
        setSelectedId(latest.selected_option_id ?? latest.options[0]?.option_id ?? '')
      }
    }).catch((error: unknown) => {
      if (!cancelled && !composingNewRef.current) {
        setApiError(error instanceof Error ? error.message : '无法读取现有差旅任务')
      }
    })
    return () => { cancelled = true }
  }, [])

  const displayOptions = useMemo(() => {
    const lodgingRequirement = lodgingRequirementFromTask(task)
    return task?.options.map((option, index) => displayOptionFromApi(option, index, lodgingRequirement)) ?? []
  }, [task])
  const selected = displayOptions.find((option) => option.id === selectedId) ?? displayOptions[0]
  const selectedApiOption = task?.options.find((option) => option.option_id === selected?.id) ?? null
  const taskState = task ? getStateMeta(task.state) : null
  // 任务处于澄清/结构化输入态且非「新建中」时，切换到澄清相关 UI
  const needsClarification = Boolean(
    !composingNew
    && task
    && (task.state === 'NEEDS_CLARIFICATION' || task.state === 'NEEDS_STRUCTURED_INPUT'),
  )
  const origin = intentText(task, 'origin', '')
  const destination = intentText(task, 'destination', '')
  const departureAfter = intentText(task, 'departure_after', '')
  const departureWall = departureAfter ? parseIsoWallClock(departureAfter) : null
  const validDepartureDate = departureWall
    ? new Date(Date.UTC(departureWall.year, departureWall.month - 1, departureWall.day))
    : null

  /** 用最新任务快照刷新本地选中态与页签。 */
  const applyTask = (nextTask: TripTask) => {
    setTask(nextTask)
    const nextSelected = nextTask.selected_option_id ?? nextTask.options[0]?.option_id ?? ''
    setSelectedId(nextSelected)
    setCompared([])
    setTab('options')
  }

  /** 提交自然语言：澄清态走 messages，否则创建新任务。 */
  const submitInstruction = async (message: string): Promise<boolean> => {
    if (!user) return false
    writeComposerDraft(message)
    setApiBusy(true)
    setApiError('')
    try {
      const isClarification = !composingNew
        && task
        && (task.state === 'NEEDS_CLARIFICATION' || task.state === 'NEEDS_STRUCTURED_INPUT')
      const nextTask = isClarification
        ? task.intent_entrypoint === 'legacy'
          ? await api.submitLegacyMessage(task.task_id, message)
          : await api.submitSemanticMessage(task.task_id, message)
        : await api.createLegacyNaturalLanguage(message, user.employee_id ?? user.user_id)
      setInstruction(message)
      composingNewRef.current = false
      setComposingNew(false)
      clearComposerDraft()
      setDraftMessage('')
      applyTask(nextTask)
      onToast(`真实 API 已更新任务 ${nextTask.task_id.slice(0, 8)} · ${getStateMeta(nextTask.state).label}`)
      return true
    } catch (error) {
      const status = error instanceof ApiError ? error.status : 0
      if (status === 404 && task) {
        composingNewRef.current = true
        setTask(null)
        setSelectedId('')
        setComposingNew(true)
        setDraftMessage(message)
        writeComposerDraft(message)
        setApiError('当前任务已从服务器消失。开发服务重启后内存任务会清空，请重新发送完整指令，不要继续补这一条。')
        return false
      }
      const messageText = error instanceof ApiError && error.status === 503
        ? `API 暂不可用：${error.message}`
        : error instanceof Error ? error.message : '创建差旅任务失败'
      setApiError(messageText)
      writeComposerDraft(message)
      setDraftMessage(message)
      return false
    } finally {
      setApiBusy(false)
    }
  }

  /** 切换方案对比勾选（最多 3 个）。 */
  const toggleCompare = (id: string) => {
    setCompared((current) => current.includes(id) ? current.filter((item) => item !== id) : current.length < 3 ? [...current, id] : current)
  }

  /** 确认当前选中方案并提交到后端（可能触发审批）。 */
  const confirmSelection = async () => {
    if (!task || !selected) return
    setActionBusy(true)
    setApiError('')
    try {
      const nextTask = await api.selectOption(task.task_id, selected.id, businessReason.trim() || undefined)
      applyTask(nextTask)
      onToast(`方案已提交到 API · ${getStateMeta(nextTask.state).label}`)
    } catch (error) {
      setApiError(error instanceof Error ? error.message : '提交方案失败')
    } finally {
      setActionBusy(false)
    }
  }

  /** 提交结构化行程表单并继续搜索。 */
  const submitStructured = async (payload: StructuredTripCreate): Promise<boolean> => {
    if (!task) return false
    setApiBusy(true)
    setApiError('')
    try {
      const nextTask = await api.submitStructuredRequest(task.task_id, payload)
      composingNewRef.current = false
      setComposingNew(false)
      applyTask(nextTask)
      onToast(`结构化表单已提交 · ${getStateMeta(nextTask.state).label}`)
      return true
    } catch (error) {
      setApiError(error instanceof Error ? error.message : '提交结构化行程失败')
      return false
    } finally {
      setApiBusy(false)
    }
  }

  /** 请求后端对该任务重新规划候选方案。 */
  const replan = async () => {
    if (!task) return
    setActionBusy(true)
    setApiError('')
    try {
      const nextTask = await api.replan(task.task_id)
      applyTask(nextTask)
      onToast(`API 已重新规划 · ${getStateMeta(nextTask.state).label}`)
    } catch (error) {
      setApiError(error instanceof Error ? error.message : '重新规划失败')
    } finally {
      setActionBusy(false)
    }
  }

  return (
    <div className="page plan-page">
      <div className="page-heading compact">
        <div><div className="breadcrumb">智能规划 {task && <><span>/</span> {task.task_id}</>}</div><h1>{origin && destination ? `${origin}到${destination}差旅` : '创建新的差旅行程'}</h1><p>{task ? `${taskState?.description} · 行程申请 v${task.request_version ?? 1}` : '输入自然语言指令，结果将直接来自本地 FastAPI 服务。'}</p></div>
        <div className="heading-status"><Badge tone={stateBadgeTone(task?.state ?? null)}>{task && <span className="pulse" />}{taskState?.label ?? '等待指令'}</Badge><button className="more-button" aria-label="更多任务操作">•••</button></div>
      </div>

      {/* 状态机分支：结构化表单 → 澄清面板 → 自然语言作曲器 */}
      {task && task.state === 'NEEDS_STRUCTURED_INPUT'
        ? <StructuredRequestForm task={task} busy={apiBusy} error={apiError} onSubmit={submitStructured} />
        : needsClarification && task
          ? <ClarificationPanel task={task} busy={apiBusy} error={apiError} onSubmit={submitInstruction} />
          : <InlineAgentComposer task={task} busy={apiBusy} error={apiError} message={draftMessage} onMessageChange={updateDraft} onSubmit={submitInstruction} />}

      {task && origin && destination && <section className="ticket-strip">
        <div className="ticket-date"><span>{validDepartureDate ? new Intl.DateTimeFormat('en-US', { month: 'short', timeZone: 'UTC' }).format(validDepartureDate).toUpperCase() : 'DATE'}</span><strong>{validDepartureDate ? validDepartureDate.getUTCDate() : '—'}</strong><small>{validDepartureDate ? new Intl.DateTimeFormat('zh-CN', { weekday: 'short', timeZone: 'UTC' }).format(validDepartureDate) : '日期待补充'}</small></div>
        <div className="ticket-route">
          <div><small>出发地</small><strong>{origin}</strong><span>ORIGIN</span></div>
          <div className="ticket-track"><i /><span><Icon name="arrow" size={17} /></span><i /></div>
          <div><small>目的地</small><strong>{destination}</strong><span>DESTINATION</span></div>
        </div>
        <div className="ticket-meta"><span><Icon name="clock" size={16} />最晚抵达 {intentText(task, 'arrive_by')}</span><span><Icon name="briefcase" size={16} />{taskState?.description}</span></div>
        <div className="ticket-seal"><Icon name="shield" size={20} /><b>LIVE API</b><span>{task.task_id.slice(0, 8)}</span><small>已同步</small></div>
      </section>}

      {!task && <section className="task-empty-state"><span><Icon name="spark" size={22} /></span><div><b>还没有真实差旅任务</b><p>在上方输入指令后，前端会调用 <code>POST /trip-tasks</code>，并用 API 返回内容替换这里。</p></div></section>}

      {task && displayOptions.length === 0 && !needsClarification && <section className="task-progress-panel">
        <span className={apiBusy ? 'progress-orbit spinning' : 'progress-orbit'}><Icon name={task.failure ? 'info' : 'spark'} /></span>
        <div><div className="section-kicker">{task.state}</div><h2>{taskState?.label}</h2><p>{searchOutcomeCopy(task, taskState?.description)}</p>{task.missing_required_fields.length > 0 && <div className="missing-fields">待补充：{task.missing_required_fields.join('、')}</div>}</div>
      </section>}

      {task && displayOptions.length > 0 && <>
        <div className="content-tabs" role="tablist">
          <button className={tab === 'options' ? 'active' : ''} onClick={() => setTab('options')}>推荐方案 <em>{displayOptions.length}</em></button>
          <button className={tab === 'request' ? 'active' : ''} onClick={() => setTab('request')}>需求与偏好</button>
          <button className={tab === 'timeline' ? 'active' : ''} onClick={() => setTab('timeline')}>工具记录</button>
        </div>

        {tab === 'options' && selected && <div className="plan-layout">
          <main className="option-list">
            {originalInstruction(task, instruction) && <div className="instruction-quote">
              <span>原始指令</span>
              <blockquote>“{originalInstruction(task, instruction)}”</blockquote>
            </div>}
            <div className="assistant-note">
              <span className="assistant-mark"><Icon name="spark" /></span>
              <div>
                <b>API 返回 {displayOptions.length} 个可行方案</b>
                <p>结果来自 <strong>{selectedApiOption?.outbound.provider}</strong> 库存，并已通过后端政策引擎评估。</p>
                {Array.isArray(task.intent_fields.soft_preferences) && task.intent_fields.soft_preferences.includes('hotel_near_client') && selectedApiOption?.hotel && (selectedApiOption.hotel.commute_known === false || selectedApiOption.hotel.commute_minutes >= 1440) && (
                  <p>你提到希望酒店靠近客户公司，但未提供客户地址。当前酒店是目的地城市报价，<strong>不能按通勤距离筛选</strong>。</p>
                )}
              </div>
              <button onClick={() => setTab('timeline')}>查看工具记录</button>
            </div>
            <div className="list-toolbar"><p>按后端综合评分排序</p><span className="live-data-label"><i />LIVE DATA</span></div>
            {displayOptions.map((option) => <OptionCard key={option.id} option={option} selected={selected.id === option.id} compared={compared.includes(option.id)} onSelect={() => setSelectedId(option.id)} onCompare={() => toggleCompare(option.id)} />)}
            <button className="replan-button" disabled={actionBusy} onClick={() => void replan()}><Icon name="spark" />{actionBusy ? '正在调用 API…' : '这些都不合适？调用 API 重新规划'}</button>
          </main>

          <aside className="decision-panel">
            <div className="decision-title"><span>当前选择</span><Badge tone={selected.policyTone}>{selected.policy}</Badge></div>
            <h3>{selected.number} + {selected.hotel}</h3>
            <div className="price-breakdown"><div><span>往返交通</span><b>{selected.transportPrice}</b></div><div><span>酒店</span><b>{selected.hotelPrice}</b></div><div className="total"><span>预估总计</span><strong>{selected.currencySymbol}{selected.price}</strong></div></div>
            <div className={`policy-callout ${selected.policyTone}`}>
              <span><Icon name={selected.policyTone === 'ok' ? 'shield' : 'info'} size={18} /></span>
              <div><b>{selected.policyTone === 'ok' ? '后端政策校验通过' : '选择后需要审批或补充证据'}</b><p>{selected.policyTone === 'ok' ? '以下结论来自当前任务固定的政策证据。' : '请填写业务原因，后端将创建审批请求。'}</p></div>
            </div>
            <ul className="evidence-list">
              {(selectedApiOption?.rule_evidence ?? []).slice(0, 5).map((rule) => <li key={rule.rule_id}><span>{rule.message || rule.rule_id}</span><b className={rule.outcome === 'COMPLIANT' ? 'pass' : ''}>{rule.outcome === 'COMPLIANT' ? '通过' : rule.outcome === 'REQUIRES_APPROVAL' ? '需审批' : '未通过'}</b></li>)}
              <li><span>库存引用</span><b>{selectedApiOption?.inventory_refs.length ?? 0} 条</b></li>
            </ul>
            {selected.policyTone !== 'ok' && <label className="business-reason"><span>业务原因（审批必填）</span><textarea data-testid="business-reason" value={businessReason} onChange={(event) => setBusinessReason(event.target.value)} placeholder="说明为什么需要选择该方案" /></label>}
            <button className="primary full" data-testid="confirm-selection" disabled={actionBusy || (selected.policyTone !== 'ok' && !businessReason.trim())} onClick={() => void confirmSelection()}>{actionBusy ? '正在提交 API…' : selected.policyTone === 'ok' ? '确认并重新验证' : '选择并申请审批'}{!actionBusy && <Icon name="arrow" />}</button>
            <p className="decision-help"><Icon name="info" size={14} />操作会写入真实任务状态，但预订和支付能力仍被后端禁用。</p>
          </aside>
        </div>}

        {tab === 'request' && <RequestPanel task={task} rawMessage={originalInstruction(task, instruction)} />}
        {tab === 'timeline' && <TimelinePanel task={task} />}

        {compared.length > 1 && <div className="compare-dock"><span>已选择 <b>{compared.length}</b> 个方案</span><div>{compared.map((id) => <Badge key={id} tone="dark">{displayOptions.find((item) => item.id === id)?.number}</Badge>)}</div><button className="primary small" onClick={() => onToast('已选方案来自当前 API 响应')}>确认对比</button><button className="dock-close" onClick={() => setCompared([])}><Icon name="close" /></button></div>}
      </>}
    </div>
  )
}

/** 「需求与偏好」页签：展示解析出的意图字段。 */
function RequestPanel({ task, rawMessage }: { task: TripTask; rawMessage: string }) {
  const fields = [
    ['出发地', intentText(task, 'origin')],
    ['目的地', intentText(task, 'destination')],
    ['最早出发', intentText(task, 'departure_after')],
    ['最晚抵达', intentText(task, 'arrive_by')],
    ['返程开始', intentText(task, 'return_after')],
    ['住宿需求', lodgingRequirementLabel(task)],
    ['酒店入住', intentText(task, 'hotel_check_in')],
    ['酒店退房', intentText(task, 'hotel_check_out')],
  ]
  const preferences = [...(Array.isArray(task.intent_fields.hard_constraints) ? task.intent_fields.hard_constraints : []), ...(Array.isArray(task.intent_fields.soft_preferences) ? task.intent_fields.soft_preferences : [])]
  return <section className="detail-panel two-col">
    <div><div className="section-kicker">API 解析结果</div><h2>“{rawMessage || '该任务从后端存储中恢复'}”</h2><div className="request-meta"><span><Icon name="user" />任务 {task.task_id.slice(0, 8)}</span><span><Icon name="clock" />第 {task.request_version ?? 0} 版需求</span></div></div>
    <div className="field-grid">{fields.map(([label, value]) => <label key={label}>{label}<b>{value}</b></label>)}</div>
    <div className="preference-block"><h3>约束、偏好与假设</h3><div>{preferences.map((value) => <Badge key={String(value)} tone="gray">{String(value)}</Badge>)}{task.assumptions.map((value) => <Badge key={value} tone="orange">假设 · {value}</Badge>)}{preferences.length === 0 && task.assumptions.length === 0 && <span className="muted-copy">暂无额外约束</span>}</div></div>
  </section>
}

/** 「工具记录」页签：展示工具调用预算与调用时间线。 */
function TimelinePanel({ task }: { task: TripTask }) {
  const calls = task.tool_budget.calls
  return <section className="detail-panel"><div className="timeline-head"><div><div className="section-kicker">真实工具调用记录</div><h2>本次任务使用 {task.tool_budget.used} / {task.tool_budget.limit} 次工具调用</h2></div><Badge tone={task.tool_budget.blocked ? 'warn' : 'green'}>{task.tool_budget.blocked ? '预算耗尽' : `剩余 ${task.tool_budget.remaining}`}</Badge></div>{calls.length > 0 ? <div className="timeline">{calls.map((call, index) => <div key={`${call.sequence}-${call.tool_name}`}><time>{timeText(call.started_at)}</time><i className={index === calls.length - 1 ? 'current' : ''} /><section><b>{call.tool_name}</b><p>{call.status}{call.reason_code ? ` · ${call.reason_code}` : ''}{call.error_code ? ` · ${call.error_code}` : ''}</p></section></div>)}</div> : <div className="timeline-empty">该任务暂时没有工具调用记录。</div>}</section>
}

/** 差旅列表视图：全部行来自 `GET /trip-tasks`，不含演示数据。 */
function TripsView({ mode, onOpen }: { mode: WorkspaceRole; onOpen: (() => void) | null }) {
  const [rows, setRows] = useState<TaskSummary[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    api.listTasks({ summary: true, limit: 100 })
      .then((items) => {
        if (cancelled) return
        setRows(items as TaskSummary[])
        setError('')
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : '读取任务列表失败')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [])

  const copy = {
    employee: { eyebrow: 'MY TRAVEL', title: '我的差旅', description: '集中查看本人规划、审批与交接状态。' },
    approver: { eyebrow: 'TEAM TRAVEL', title: '团队差旅', description: '查看直属团队的差旅状态，不在此处修改员工申请。' },
    admin: { eyebrow: 'TASK OPERATIONS', title: '全局任务总览', description: '监控全部差旅任务和异常状态；审批决定仍由直属审批人执行。' },
  }[mode]

  // 计数直接由返回行统计，不做无数据来源的估算指标。
  const waitingUser = rows.filter((item) => item.state === 'WAITING_FOR_USER').length
  const waitingApproval = rows.filter((item) => item.state === 'WAITING_FOR_APPROVAL').length
  const handedOff = rows.filter((item) => item.state === 'HANDED_OFF').length

  return <div className="page">
    <div className="page-heading"><div><div className="eyebrow">{copy.eyebrow}</div><h1>{copy.title}</h1><p>{copy.description}</p></div>{onOpen && <button className="primary" onClick={onOpen}><Icon name="plus" />新建差旅</button>}</div>
    <div className="metric-row">
      <div><span>可见任务</span><b>{rows.length}</b><small>来自 <code>GET /trip-tasks</code></small></div>
      <div><span>等待你选择</span><b>{waitingUser}</b><small>状态 WAITING_FOR_USER</small></div>
      <div><span>等待审批</span><b>{waitingApproval}</b><small>状态 WAITING_FOR_APPROVAL</small></div>
      <div><span>已交接</span><b>{handedOff}</b><small>状态 HANDED_OFF</small></div>
    </div>
    <section className="table-section">
      <div className="table-toolbar"><div><h3>任务列表</h3><p>共 {rows.length} 条</p></div></div>
      {error && <div className="task-empty-state"><span><Icon name="info" size={22} /></span><div><b>读取任务列表失败</b><p>{error}</p></div></div>}
      {!error && loading && <div className="task-empty-state"><span><Icon name="spark" size={22} /></span><div><b>正在读取任务列表…</b></div></div>}
      {!error && !loading && rows.length === 0 && <div className="task-empty-state"><span><Icon name="info" size={22} /></span><div><b>还没有可见的差旅任务</b><p>列表只显示 <code>GET /trip-tasks</code> 返回的真实任务，不展示演示数据。</p></div></div>}
      {!error && !loading && rows.length > 0 && <div className="data-table trip-table">
        <div className="table-head"><span>任务编号</span><span>申请人</span><span>状态</span><span>方案数</span><span>更新时间</span><span /></div>
        {rows.map((row) => <div className="table-row readonly" key={row.task_id}>
          <span className="mono">{row.task_id.slice(0, 8)}</span>
          <span>{row.employee_id}</span>
          <span><Badge tone={stateBadgeTone(row.state)}>{getStateMeta(row.state).label}</Badge></span>
          <span>{row.option_count}</span>
          <span>{row.updated_at ? new Date(row.updated_at).toLocaleString() : '—'}</span>
          <span>{row.failure ? <small className="orange-text">{row.failure}</small> : null}</span>
        </div>)}
      </div>}
    </section>
  </div>
}

/** 审批中心：拉取收件箱、查看详情并提交批准/拒绝。 */
function ApprovalsView({ onToast }: { onToast: (text: string) => void }) {
  const [inbox, setInbox] = useState<TaskSummary[]>([])
  const [activeId, setActiveId] = useState('')
  const [detail, setDetail] = useState<TripTask | null>(null)
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const loadInbox = async (preferredId?: string) => {
    const items = await api.approvalInbox(100)
    setInbox(items)
    const nextId = preferredId && items.some((item) => item.task_id === preferredId)
      ? preferredId
      : items[0]?.task_id ?? ''
    setActiveId(nextId)
    return nextId
  }

  useEffect(() => {
    let cancelled = false
    loadInbox().then((taskId) => {
      if (!cancelled && !taskId) setDetail(null)
    }).catch((loadError: unknown) => {
      if (!cancelled) setError(loadError instanceof Error ? loadError.message : '无法读取审批队列')
    })
    return () => { cancelled = true }
  }, [])

  useEffect(() => {
    if (!activeId) {
      setDetail(null)
      return
    }
    let cancelled = false
    api.getTask(activeId).then((task) => {
      if (!cancelled) setDetail(task)
    }).catch((loadError: unknown) => {
      if (!cancelled) setError(loadError instanceof Error ? loadError.message : '无法读取审批任务')
    })
    return () => { cancelled = true }
  }, [activeId])

  const selected = detail?.options.find((option) => option.option_id === detail.selected_option_id) ?? null
  const decide = async (approved: boolean) => {
    if (!activeId || !reason.trim()) return
    setBusy(true)
    setError('')
    try {
      const next = await api.decideApproval(activeId, approved, reason.trim())
      onToast(approved ? `已批准 ${next.task_id.slice(0, 8)} · ${getStateMeta(next.state).label}` : `已拒绝 ${next.task_id.slice(0, 8)}`)
      setReason('')
      const remaining = await loadInbox()
      if (!remaining) setDetail(null)
    } catch (decideError) {
      setError(decideError instanceof Error ? decideError.message : '提交审批决定失败')
    } finally {
      setBusy(false)
    }
  }

  return <div className="page" data-testid="approvals-view">
    <div className="page-heading"><div><div className="eyebrow">APPROVAL INBOX</div><h1>审批中心</h1><p>只展示指派给你的待办，批准身份来自登录会话。</p></div><Badge tone={inbox.length ? 'orange' : 'gray'}>{inbox.length} 项待处理</Badge></div>
    {error && <div className="clarification-error" role="alert"><Icon name="info" size={15} />{error}</div>}
    {inbox.length === 0 ? <section className="task-empty-state"><span><Icon name="check" size={22} /></span><div><b>没有待你处理的审批</b><p>队列来自 <code>GET /approvals/inbox</code>，不会显示演示数据。</p></div></section> : <div className="approval-layout">
      <section className="approval-queue"><div className="queue-title"><b>待我审批</b></div>{inbox.map((item) => <button key={item.task_id} data-testid={`approval-item-${item.task_id}`} className={activeId === item.task_id ? 'active' : ''} onClick={() => setActiveId(item.task_id)}><span className="avatar warm">{item.employee_id.slice(0, 2)}</span><span><b>{item.employee_id}</b><small>{item.task_id}</small><em>{item.state}</em></span></button>)}</section>
      <section className="approval-detail">
        {detail && selected ? <>
          <div className="approval-title"><div><div className="section-kicker">{detail.task_id}</div><h2>{selected.outbound.origin} → {selected.outbound.destination}</h2><p>{detail.approval?.employee_snapshot_id ?? '申请人'} · 直属经理 {detail.approval?.approver_id}</p></div><Badge tone="orange">{getStateMeta(detail.state).label}</Badge></div>
          <div className="approval-route"><div><small>出发</small><b>{selected.outbound.origin}</b><span>{travelTimeText(selected.outbound.depart_at)}</span></div><span><Icon name={selected.outbound.mode === 'TRAIN' ? 'train' : 'plane'} /><i /></span><div><small>到达</small><b>{selected.outbound.destination}</b><span>{travelTimeText(selected.outbound.arrive_at)}</span></div><div className="approval-cost"><small>申请总额</small><b>{amountText(selected.total_cost, selected.currency)}</b></div></div>
          <div className="approval-grid"><div><span>业务目的</span><b>{detail.approval?.business_reason || '未填写'}</b></div><div><span>政策结论</span><b>{selected.policy_outcome}</b></div><div><span>库存引用</span><b>{selected.inventory_refs.length} 条</b></div><div><span>审批过期</span><b>{detail.approval?.expires_at ? dateText(detail.approval.expires_at) : '—'}</b></div></div>
          <div className="exception-box"><span><Icon name="info" /></span><div><b>政策例外需要你判断</b><p>{(selected.rule_evidence.find((rule) => rule.outcome === 'REQUIRES_APPROVAL')?.message) || '该方案超过自动通过阈值。'}</p><div className="reason-quote"><span>申请人说明</span>“{detail.approval?.business_reason || '无'}”</div></div></div>
          <label className="decision-reason"><span>审批意见</span><textarea data-testid="approval-reason" value={reason} onChange={(event) => setReason(event.target.value)} placeholder="请填写通过或拒绝的原因（必填）" /></label>
          <div className="approval-actions"><button className="danger-outline" data-testid="approval-reject" disabled={busy || !reason.trim()} onClick={() => void decide(false)}>拒绝申请</button><button className="primary" data-testid="approval-approve" disabled={busy || !reason.trim()} onClick={() => void decide(true)}><Icon name="check" />批准例外</button></div>
        </> : <div className="task-empty-state"><span><Icon name="info" size={22} /></span><div><b>正在读取任务详情</b></div></div>}
      </section>
    </div>}
  </div>
}

/** 差旅政策浏览页：内容来自 `GET /policy` 的当前生效快照。 */
function PolicyView({ mode }: { mode: WorkspaceRole }) {
  const [policy, setPolicy] = useState<ActivePolicy | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    api.activePolicy()
      .then((item) => {
        if (cancelled) return
        setPolicy(item)
        setError('')
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : '读取政策失败')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [])

  return <div className="page">
    <div className="page-heading"><div><div className="eyebrow">POLICY LIBRARY</div><h1>{mode === 'admin' ? '政策管理' : '差旅政策'}</h1><p>{mode === 'admin' ? '查看当前生效版本、规则范围和固定内容指纹。' : '查看与你或团队差旅相关的确定性规则。'}</p></div></div>
    {error && <div className="task-empty-state"><span><Icon name="info" size={22} /></span><div><b>读取政策失败</b><p>{error}</p></div></div>}
    {!error && loading && <div className="task-empty-state"><span><Icon name="spark" size={22} /></span><div><b>正在读取当前生效政策…</b></div></div>}
    {policy && <>
      <section className="policy-hero">
        <div><Badge tone="green">生效中</Badge><h2>{policy.policy_version}</h2><p>快照 {policy.snapshot_id} · 自 {policy.effective_from} 起{policy.effective_to ? ` 至 ${policy.effective_to}` : ''}</p></div>
        <div className="policy-hash"><span>内容指纹</span><b>{policy.content_hash ? `${policy.content_hash.slice(0, 8)}…${policy.content_hash.slice(-4)}` : '未记录'}</b><small>固定后不可修改</small></div>
        <div><span>{mode === 'employee' ? '你的适用等级' : '覆盖等级'}</span><b>{mode === 'employee' ? (policy.viewer_level ?? '未知') : `${policy.level_rules.length} 个等级`}</b><small>抵达缓冲 {policy.arrival_buffer_minutes} 分钟 · 结算币种 {policy.currency}</small></div>
      </section>
      <section className="table-section">
        <div className="table-toolbar"><div><h3>职级舱等规则</h3><p>共 {policy.level_rules.length} 条</p></div></div>
        <div className="data-table policy-table"><div className="table-head"><span>职级</span><span>允许航班舱等</span><span>允许火车席别</span><span /></div>
          {policy.level_rules.map((rule) => <div className="table-row readonly" key={rule.level}>
            <span className="mono">{rule.level}{rule.level === policy.viewer_level ? ' ·' : ''}</span>
            <span>{rule.allowed_flight_classes.join('、') || '—'}</span>
            <span>{rule.allowed_train_classes.join('、') || '—'}</span>
            <span />
          </div>)}
        </div>
      </section>
      <section className="table-section">
        <div className="table-toolbar"><div><h3>酒店城市上限</h3><p>共 {policy.hotel_city_caps.length} 条 · 单位 {policy.currency}/晚</p></div></div>
        <div className="data-table policy-table"><div className="table-head"><span>城市</span><span>每晚上限</span><span /><span /></div>
          {policy.hotel_city_caps.map((cap) => <div className="table-row readonly" key={cap.city}>
            <span>{cap.city}</span>
            <span><b>{currencySymbol(policy.currency)}{cap.nightly_cap}</b></span>
            <span /><span />
          </div>)}
        </div>
      </section>
      <section className="table-section">
        <div className="table-toolbar"><div><h3>可发起例外审批的规则</h3><p>共 {policy.exception_allowed_rule_ids.length} 条</p></div></div>
        <div className="data-table policy-table"><div className="table-head"><span>规则 ID</span><span>例外机制</span><span /><span /></div>
          {policy.exception_allowed_rule_ids.map((ruleId) => <div className="table-row readonly" key={ruleId}>
            <span className="mono">{ruleId}</span>
            <span><Badge tone="orange">可例外</Badge></span>
            <span /><span />
          </div>)}
          {policy.exception_allowed_rule_ids.length === 0 && <div className="table-row readonly"><span>当前快照没有允许例外的规则</span><span /><span /><span /></div>}
        </div>
      </section>
    </>}
    <div className="policy-note"><Icon name="info" /><div><b>政策结论由确定性规则引擎生成</b><p>AI 只负责解释字段与自然语言，不会批准例外或修改政策判断。每次行程都会固定政策版本和证据。</p></div></div>
  </div>
}

/** 审计与系统页：事件来自 `GET /audit-events`，服务状态来自 `GET /health`。 */
function AuditView() {
  const [auditTab, setAuditTab] = useState<'events' | 'system'>('events')
  const [events, setEvents] = useState<AuditEvent[]>([])
  const [health, setHealth] = useState<HealthResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    Promise.all([api.recentAuditEvents(50), api.health()])
      .then(([items, healthResponse]) => {
        if (cancelled) return
        setEvents(items)
        setHealth(healthResponse)
        setError('')
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : '读取审计数据失败')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [])

  return <div className="page">
    <div className="page-heading"><div><div className="eyebrow">EVIDENCE & OPERATIONS</div><h1>审计与系统</h1><p>查看任务证据、库存快照与服务运行边界。</p></div></div>
    <div className="audit-summary">
      <div><span className="health-dot" /><p>系统状态<b>{health ? health.status : '读取中'}</b></p></div>
      <div><span>库存提供方</span><b>{health ? `${health.travel_provider} · ${health.travel_provider_mode}` : '—'}</b></div>
      <div><span>预订能力</span><b className="orange-text">{health ? health.booking_capability : '—'}</b></div>
      <div><span>近期审计事件</span><b>{events.length}</b></div>
    </div>
    <div className="content-tabs slim"><button className={auditTab === 'events' ? 'active' : ''} onClick={() => setAuditTab('events')}>任务审计事件</button><button className={auditTab === 'system' ? 'active' : ''} onClick={() => setAuditTab('system')}>系统与提供方</button></div>
    {error && <div className="task-empty-state"><span><Icon name="info" size={22} /></span><div><b>读取审计数据失败</b><p>{error}</p><p>跨任务审计事件仅对管理员开放。</p></div></div>}
    {!error && auditTab === 'events' && <section className="audit-log">
      {loading && <div className="task-empty-state"><span><Icon name="spark" size={22} /></span><div><b>正在读取审计事件…</b></div></div>}
      {!loading && events.length === 0 && <div className="task-empty-state"><span><Icon name="info" size={22} /></span><div><b>暂无审计事件</b><p>事件来自 <code>GET /audit-events</code>，不会显示演示数据。</p></div></div>}
      {!loading && events.map((event) => <div className="audit-row" key={event.event_id}>
        <time>{new Date(event.created_at).toLocaleString()}</time>
        <span className="event-icon event-0"><Icon name="shield" size={16} /></span>
        <div><b>{event.event_type}</b><small className="mono">{event.actor_type}</small></div>
        <span className="mono">{event.task_id.slice(0, 8)}</span>
        <span>{event.evidence_refs.length} 条证据引用</span>
        <span className="hash-button">{event.output_hash.slice(0, 8)}…</span>
      </div>)}
    </section>}
    {!error && auditTab === 'system' && <SystemPanel health={health} />}
  </div>
}

/** 系统与提供方能力边界面板；状态取自 `GET /health`。 */
function SystemPanel({ health }: { health: HealthResponse | null }) {
  const services: [string, string, string][] = health
    ? [
      ['API 服务', health.status, 'FastAPI'],
      ['语言模型', health.language_model, health.language_model_status ?? '—'],
      ['库存提供方', health.travel_provider, health.travel_provider_mode],
      ['数据持久化', health.persistence, '任务与审计仓储'],
      ['认证', health.authentication, '资源级授权'],
      ['生效政策', health.active_policy_snapshot, health.policy_config_version],
    ]
    : []
  return <section className="system-panel">
    <div className="system-boundary"><Icon name="shield" size={24} /><div><b>安全运行边界</b><p>本系统只负责规划、合规校验、审批与官方平台交接。预订、支付、退改签均不可用。</p></div><Badge tone="dark">BOOKING DISABLED</Badge></div>
    {services.length === 0
      ? <div className="task-empty-state"><span><Icon name="info" size={22} /></span><div><b>正在读取服务状态…</b></div></div>
      : <div className="service-grid">{services.map(([name, status, detail]) => <div key={name}><span><i />{name}</span><b>{status}</b><small>{detail}</small></div>)}</div>}
  </section>
}

/**
 * 根组件：等待认证 → 登录 → 按角色渲染侧栏与当前视图。
 * `composerEpoch` 用于「新建差旅」时强制重置规划页作曲器。
 */
function App() {
  const { ready, authEnabled, user, logout } = useAuth()
  const [activeView, setActiveView] = useState<View>('plan')
  const [toast, setToast] = useState('')
  const [composerEpoch, setComposerEpoch] = useState(0)

  const showToast = (text: string) => {
    setToast(text)
    window.setTimeout(() => setToast(''), 3200)
  }

  const startNewTrip = () => {
    setActiveView('plan')
    setComposerEpoch((current) => current + 1)
    window.setTimeout(() => {
      const input = document.getElementById('inline-trip-request')
      input?.scrollIntoView({ behavior: 'smooth', block: 'center' })
      input?.focus()
    }, 50)
  }

  // 认证门闸：未就绪显示加载；无用户显示登录
  if (!ready) return <LoadingScreen />
  if (!user) return <LoginScreen />

  const role = workspaceRole(user)
  const views = allowedViews(user)
  // 当前视图若不在角色权限内，回退到默认视图
  const visibleView = views.includes(activeView) ? activeView : defaultView(user)
  const canPlan = views.includes('plan')

  return (
    <div className="app-shell">
      <Sidebar activeView={visibleView} onChange={setActiveView} user={user} onLogout={authEnabled ? logout : null} />
      <div className="workspace">
        <Header activeView={visibleView} onNewTrip={canPlan ? startNewTrip : null} user={user} onLogout={authEnabled ? logout : null} />
        {/* 按可见视图切换主内容区；plan 用 hidden 保活以便新建差旅重置 */}
        {canPlan && <div className="workspace-view" hidden={visibleView !== 'plan'}><PlanView onToast={showToast} composerEpoch={composerEpoch} /></div>}
        {visibleView === 'trips' && <TripsView mode={role} onOpen={canPlan ? startNewTrip : null} />}
        {visibleView === 'approvals' && <ApprovalsView onToast={showToast} />}
        {visibleView === 'policy' && <PolicyView mode={role} />}
        {visibleView === 'audit' && <AuditView />}
      </div>
      {toast && <div className="toast" role="status"><span><Icon name="check" /></span>{toast}</div>}
    </div>
  )
}

export default App
