import { useEffect, useRef, useState } from 'react'
import { Badge, Button, Drawer, Empty, List, Progress, Space, Tag, Tooltip, Typography } from 'antd'
import { ThunderboltOutlined } from '@ant-design/icons'

import { taskApi } from '@/api/endpoints'
import type { GenerationTask } from '@/api/types'

const ACTIVE = new Set(['pending', 'running'])
const KIND_LABEL: Record<string, string> = { text: '文本', image: '图片', video: '视频', export: '导出' }

const POLL_ACTIVE = 2500
const POLL_IDLE = 10000

/**
 * 全局生成进度浮层：右下角常驻按钮 + 右侧抽屉。
 * 有新任务开始自动弹出，全部结束后自动收起；空闲时降低轮询频率。
 */
export default function TaskProgressDrawer() {
  const [tasks, setTasks] = useState<GenerationTask[]>([])
  const [open, setOpen] = useState(false)
  const knownRef = useRef<Set<number>>(new Set())
  const autoOpenedRef = useRef(false)

  useEffect(() => {
    let cancelled = false
    let timer: number | undefined

    const tick = async () => {
      let active: GenerationTask[] = []
      try {
        const all = await taskApi.list()
        if (cancelled) return
        active = all.filter((task) => ACTIVE.has(task.status))

        const hasNew = active.some((task) => !knownRef.current.has(task.id))
        active.forEach((task) => knownRef.current.add(task.id))

        if (hasNew && !autoOpenedRef.current) {
          autoOpenedRef.current = true
          setOpen(true)
        }
        if (active.length === 0 && autoOpenedRef.current) {
          autoOpenedRef.current = false
          setOpen(false)
        }
        setTasks(active)
      } catch {
        // 轮询失败静默处理，等待下一个周期
      }
      if (!cancelled) {
        timer = window.setTimeout(tick, active.length ? POLL_ACTIVE : POLL_IDLE)
      }
    }

    void tick()
    return () => {
      cancelled = true
      if (timer) window.clearTimeout(timer)
    }
  }, [])

  const close = () => {
    autoOpenedRef.current = false
    setOpen(false)
  }

  return (
    <>
      <Tooltip title={tasks.length ? `${tasks.length} 个任务执行中` : '生成进度'} placement="left">
        <Badge count={tasks.length} size="small" offset={[-2, 2]}>
          <Button
            className="task-fab"
            shape="circle"
            size="large"
            type={tasks.length ? 'primary' : 'default'}
            icon={<ThunderboltOutlined />}
            onClick={() => {
              autoOpenedRef.current = false
              setOpen(true)
            }}
          />
        </Badge>
      </Tooltip>

      <Drawer title="生成进度" open={open} onClose={close} width={380}>
        {tasks.length === 0 ? (
          <Empty description="当前没有正在执行的任务" image={Empty.PRESENTED_IMAGE_SIMPLE} />
        ) : (
          <List
            dataSource={tasks}
            renderItem={(task) => (
              <List.Item style={{ display: 'block' }}>
                <Space style={{ width: '100%', justifyContent: 'space-between' }}>
                  <Space size={6}>
                    <Tag style={{ margin: 0 }}>{KIND_LABEL[task.kind] ?? task.kind}</Tag>
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                      #{task.id}
                    </Typography.Text>
                  </Space>
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    {task.provider ?? ''}
                    {task.model ? ` · ${task.model}` : ''}
                  </Typography.Text>
                </Space>
                <Progress
                  percent={task.progress}
                  size="small"
                  status={task.status === 'pending' ? 'normal' : 'active'}
                  style={{ marginTop: 10 }}
                />
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  {task.status === 'pending' ? '排队中…' : '执行中…'}
                </Typography.Text>
              </List.Item>
            )}
          />
        )}
      </Drawer>
    </>
  )
}
