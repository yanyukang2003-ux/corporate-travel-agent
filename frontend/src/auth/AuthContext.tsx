/**
 * 认证上下文：管理登录态、健康检查与角色标记。
 * 在认证关闭时提供开发用合成身份，供整棵应用树通过 useAuth 读取。
 */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'
import { api, ApiError, setToken } from '../api/client'
import type { HealthResponse, UserIdentity } from '../api/types'

/** AuthProvider 向子组件暴露的认证与会话状态。 */
interface AuthContextValue {
  ready: boolean
  authEnabled: boolean
  user: UserIdentity | null
  health: HealthResponse | null
  error: string | null
  login: (userId: string, password: string) => Promise<void>
  logout: () => void
  refreshHealth: () => Promise<void>
  isEmployee: boolean
  isApprover: boolean
  isAdmin: boolean
}

const AuthContext = createContext<AuthContextValue | null>(null)

/** 后端关闭认证时使用的本地演示员工身份。 */
const developmentIdentity: UserIdentity = {
  user_id: 'E1001',
  roles: ['employee'],
  employee_id: 'E1001',
}

/**
 * 认证 Provider：启动时探测健康状态并恢复会话。
 * 监听 401 广播，在令牌失效时清空用户并提示重新登录。
 */
export function AuthProvider({ children }: { children: ReactNode }) {
  const [ready, setReady] = useState(false)
  const [authEnabled, setAuthEnabled] = useState(false)
  const [user, setUser] = useState<UserIdentity | null>(null)
  const [health, setHealth] = useState<HealthResponse | null>(null)
  const [error, setError] = useState<string | null>(null)

  /** 重新拉取 `/health`，并在认证关闭时切到开发身份。 */
  const refreshHealth = useCallback(async () => {
    setError(null)
    try {
      const h = await api.health()
      setHealth(h)
      const enabled = h.authentication === 'enabled'
      setAuthEnabled(enabled)
      if (!enabled) setUser(developmentIdentity)
    } catch (err) {
      setError(err instanceof Error ? err.message : '无法连接后端 API')
    }
  }, [])

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const h = await api.health()
        if (cancelled) return
        setHealth(h)
        const enabled = h.authentication === 'enabled'
        setAuthEnabled(enabled)

        if (!enabled) {
          // 开发模式关闭认证：使用合成员工身份做演示。
          setUser(developmentIdentity)
        } else if (localStorage.getItem('cta_access_token')) {
          try {
            const me = await api.me()
            if (!cancelled) setUser(me)
          } catch {
            setToken(null)
            if (!cancelled) setUser(null)
          }
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : '无法连接后端 API')
        }
      } finally {
        if (!cancelled) setReady(true)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  useEffect(() => {
    const handleUnauthorized = () => {
      setUser(null)
      setError('登录已过期，请重新登录')
    }
    window.addEventListener('cta:unauthorized', handleUnauthorized)
    return () => window.removeEventListener('cta:unauthorized', handleUnauthorized)
  }, [])

  /** 登录并拉取当前用户；失败时清除 token。 */
  const login = useCallback(async (userId: string, password: string) => {
    setError(null)
    try {
      const result = await api.login(userId, password)
      setToken(result.access_token)
      const me = await api.me()
      setUser(me)
      setError(null)
    } catch (err) {
      setToken(null)
      const message =
        err instanceof ApiError ? err.message : '登录失败'
      setError(message)
      throw err
    }
  }, [])

  /** 退出登录：清除本地令牌与用户态。 */
  const logout = useCallback(() => {
    setToken(null)
    setUser(null)
    setError(null)
  }, [])

  const value = useMemo<AuthContextValue>(() => {
    const roles = new Set(user?.roles ?? [])
    return {
      ready,
      authEnabled,
      user,
      health,
      error,
      login,
      logout,
      refreshHealth,
      isEmployee: roles.has('employee') || roles.has('admin'),
      isApprover: roles.has('approver') || roles.has('admin'),
      isAdmin: roles.has('admin'),
    }
  }, [ready, authEnabled, user, health, error, login, logout, refreshHealth])

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

/**
 * 读取认证上下文；必须在 AuthProvider 内使用。
 */
// oxlint-disable-next-line react/only-export-components -- provider and its hook share one private context.
export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within AuthProvider')
  return ctx
}
