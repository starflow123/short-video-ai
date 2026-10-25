import { useCallback, useEffect, useState } from 'react'
import { credentialApi } from '@/api/endpoints'
import type { Credential } from '@/api/types'

/** 加载当前用户的全部密钥，并按能力筛选。 */
export function useCredentials() {
  const [credentials, setCredentials] = useState<Credential[]>([])
  const [loading, setLoading] = useState(true)

  const refresh = useCallback(async () => {
    setLoading(true)
    try {
      setCredentials(await credentialApi.list())
    } catch {
      setCredentials([])
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const byCapability = useCallback(
    (capability: string) =>
      credentials.filter((item) => item.capabilities.split(',').includes(capability)),
    [credentials],
  )

  return { credentials, loading, refresh, byCapability }
}
