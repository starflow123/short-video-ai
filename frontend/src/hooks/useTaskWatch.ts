import { useCallback, useEffect, useRef, useState } from 'react'
import { taskApi } from '@/api/endpoints'
import type { GenerationTask } from '@/api/types'

const TERMINAL = new Set(['succeeded', 'failed', 'canceled'])

/**
 * 轮询正在执行的任务，全部结束后回调。
 * 返回 watch(taskId) 用于登记新任务。
 */
export function useTaskWatch(onFinished?: (task: GenerationTask) => void, intervalMs = 2500) {
  const [activeIds, setActiveIds] = useState<number[]>([])
  const finishedRef = useRef(onFinished)
  finishedRef.current = onFinished

  const watch = useCallback((taskId: number) => {
    setActiveIds((prev) => (prev.includes(taskId) ? prev : [...prev, taskId]))
  }, [])

  useEffect(() => {
    if (activeIds.length === 0) return
    let cancelled = false

    const tick = async () => {
      const results = await Promise.all(
        activeIds.map((id) => taskApi.get(id).catch(() => null)),
      )
      const done: number[] = []
      results.forEach((task) => {
        if (task && TERMINAL.has(task.status)) {
          done.push(task.id)
          finishedRef.current?.(task)
        }
      })
      if (!cancelled && done.length) {
        setActiveIds((prev) => prev.filter((id) => !done.includes(id)))
      }
    }

    void tick()
    const timer = window.setInterval(tick, intervalMs)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [activeIds, intervalMs])

  return { watch, activeIds }
}
