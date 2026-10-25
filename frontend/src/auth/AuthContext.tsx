import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { authApi } from '@/api/endpoints'
import { tokenStore } from '@/api/client'
import type { User } from '@/api/types'

interface AuthContextValue {
  user: User | null
  loading: boolean
  login: (username: string, password: string) => Promise<void>
  register: (username: string, password: string, email?: string, inviteCode?: string) => Promise<void>
  logout: () => void
}

const AuthContext = createContext<AuthContextValue | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let alive = true
    const bootstrap = async () => {
      if (!tokenStore.get()) {
        setLoading(false)
        return
      }
      try {
        const me = await authApi.me()
        if (alive) setUser(me)
      } catch {
        tokenStore.clear()
      } finally {
        if (alive) setLoading(false)
      }
    }
    void bootstrap()
    return () => {
      alive = false
    }
  }, [])

  const login = useCallback(async (username: string, password: string) => {
    const token = await authApi.login({ username, password })
    tokenStore.set(token.access_token)
    setUser(token.user)
  }, [])

  const register = useCallback(
    async (username: string, password: string, email?: string, inviteCode?: string) => {
      const token = await authApi.register({
        username,
        password,
        email,
        invite_code: inviteCode || undefined,
      })
      tokenStore.set(token.access_token)
      setUser(token.user)
    },
    [],
  )

  const logout = useCallback(() => {
    void authApi.logout().catch(() => undefined)
    tokenStore.clear()
    setUser(null)
  }, [])

  const value = useMemo(
    () => ({ user, loading, login, register, logout }),
    [user, loading, login, register, logout],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth 必须在 AuthProvider 内使用')
  return ctx
}
