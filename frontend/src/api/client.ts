import axios, { AxiosError } from 'axios'

const TOKEN_KEY = 'aivs.token'

export const tokenStore = {
  get: () => localStorage.getItem(TOKEN_KEY),
  set: (token: string) => localStorage.setItem(TOKEN_KEY, token),
  clear: () => localStorage.removeItem(TOKEN_KEY),
}

export const http = axios.create({ baseURL: '/api', timeout: 60000 })

http.interceptors.request.use((config) => {
  const token = tokenStore.get()
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

/** 未授权时清除本地 token，交由路由守卫跳转登录。 */
http.interceptors.response.use(
  (response) => response,
  (error: AxiosError<{ detail?: string }>) => {
    if (error.response?.status === 401) {
      tokenStore.clear()
      if (!location.pathname.startsWith('/login')) {
        location.href = '/login'
      }
    }
    return Promise.reject(error)
  },
)

/** 把后端/网络错误转成可直接展示的文案。 */
export function describeError(error: unknown): string {
  if (axios.isAxiosError(error)) {
    const detail = (error.response?.data as { detail?: unknown } | undefined)?.detail
    // FastAPI 校验失败时 detail 是数组，直接交给 React 渲染会整页崩掉
    if (Array.isArray(detail)) {
      const text = detail
        .map((item) => {
          if (typeof item === 'string') return item
          const record = item as { msg?: string; loc?: unknown[] } | null
          const field = Array.isArray(record?.loc) ? record.loc.slice(1).join('.') : ''
          return `${field ? `${field}：` : ''}${record?.msg ?? '参数不合法'}`
        })
        .join('；')
      if (text) return text.slice(0, 500)
    } else if (typeof detail === 'string' && detail.trim()) {
      return detail.slice(0, 500)
    } else if (detail && typeof detail === 'object' && 'msg' in detail) {
      return String((detail as { msg: unknown }).msg).slice(0, 500)
    }
    if (error.code === 'ECONNABORTED') return '请求超时，请稍后重试'
    if (!error.response) return '无法连接后端服务，请确认服务已启动'
    return `请求失败（${error.response.status}）`
  }
  if (error instanceof Error) return error.message
  return '未知错误'
}

/** 本地媒体地址可直接使用（开发用 Vite 代理，生产用 Nginx 代理）。 */
export function mediaUrl(url?: string | null): string | undefined {
  if (!url) return undefined
  return url
}
