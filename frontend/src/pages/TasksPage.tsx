import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Alert,
  App,
  Button,
  Card,
  Checkbox,
  Col,
  Descriptions,
  Drawer,
  Empty,
  Modal,
  Popconfirm,
  Progress,
  Row,
  Select,
  Space,
  Table,
  Tag,
  Typography,
} from 'antd'
import {
  ArrowLeftOutlined,
  DeleteOutlined,
  InboxOutlined,
  RedoOutlined,
  ReloadOutlined,
  RightOutlined,
  RobotOutlined,
  StopOutlined,
} from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import type { TableRowSelection } from 'antd/es/table/interface'
import { useNavigate, useParams } from 'react-router-dom'

import ProjectCards from '@/components/ProjectCards'
import { agentRunApi, episodeApi, projectApi, taskApi } from '@/api/endpoints'
import { describeError, mediaUrl } from '@/api/client'
import type { AgentRun, Diagnosis, GenerationTask, Project } from '@/api/types'

const KIND_LABEL: Record<string, string> = {
  text: '文本',
  image: '图片',
  video: '视频',
  export: '导出',
  edit: '剪辑',
  repair: '修复',
  diagnose: 'AI 诊断',
  cut_repair: '切点修复',
}

const DIAGNOSIS_STATUS: Record<string, { text: string; color: string }> = {
  diagnosed: { text: '已诊断', color: 'blue' },
  retried: { text: '已换凭证重跑', color: 'green' },
  needs_human: { text: '需要人工处理', color: 'orange' },
  agent_error: { text: '诊断出错', color: 'red' },
  review_unavailable: { text: '无法诊断', color: 'default' },
}

const CONFIDENCE_LABEL: Record<string, string> = {
  high: '把握大',
  medium: '把握中',
  low: '把握小',
}

const AGENT_KIND_LABEL: Record<string, string> = {
  director: '导演自检',
  diagnose: '失败诊断',
  cut_repair: '切点修复',
  organize: '素材整理',
  repair: '关键帧修复',
  visual_review: '关键帧初审',
  continuity_review: '切点审查',
}

const AGENT_STATUS_LABEL: Record<string, string> = {
  pass: '通过',
  warn: '需注意',
  fail: '不合格',
}

const AGENT_STATUS_COLOR: Record<string, string> = {
  clean: 'green',
  revised: 'blue',
  repaired: 'green',
  improved: 'cyan',
  diagnosed: 'blue',
  retried: 'green',
  suggested: 'blue',
  prioritized: 'blue',
  already_pass: 'green',
  patch_only: 'blue',
  pass: 'green',
  warn: 'orange',
  fail: 'red',
}
const STATUS: Record<string, { text: string; color: string }> = {
  pending: { text: '排队中', color: 'default' },
  running: { text: '执行中', color: 'processing' },
  succeeded: { text: '已完成', color: 'success' },
  failed: { text: '失败', color: 'error' },
  canceled: { text: '已取消', color: 'warning' },
}
const ACTIVE = new Set(['pending', 'running'])

interface ShotInfo {
  episode: string
  order: number
  title?: string | null
  sort: string
}

function formatTime(value?: string | null) {
  if (!value) return '—'
  return new Date(value).toLocaleString('zh-CN', { hour12: false })
}

/** 一级页：项目卡片列表。 */
function TasksProjects() {
  const { message } = App.useApp()
  const navigate = useNavigate()
  const [projects, setProjects] = useState<Project[]>([])
  const [tasks, setTasks] = useState<GenerationTask[]>([])
  const [loading, setLoading] = useState(true)

  const load = useCallback(async () => {
    try {
      const [projectList, taskList] = await Promise.all([projectApi.list(), taskApi.list()])
      setProjects(projectList)
      setTasks(taskList)
    } catch (error) {
      message.error(describeError(error))
    } finally {
      setLoading(false)
    }
  }, [message])

  useEffect(() => {
    void load()
  }, [load])

  // 资产（尤其是全局资产）出图不归属任何项目，以前在任务中心完全看不到，
  // 只能靠逐条取消；这里聚合成一张卡片，点进去再统一处理。
  const orphanStats = useMemo(() => {
    const items = tasks.filter((task) => task.project_id == null)
    return {
      total: items.length,
      active: items.filter((task) => ACTIVE.has(task.status)).length,
      failed: items.filter((task) => task.status === 'failed').length,
    }
  }, [tasks])

  const counts = useMemo(() => {
    const map = new Map<number, { total: number; active: number; failed: number }>()
    for (const task of tasks) {
      if (task.project_id == null) continue
      const item = map.get(task.project_id) ?? { total: 0, active: 0, failed: 0 }
      item.total += 1
      if (ACTIVE.has(task.status)) item.active += 1
      if (task.status === 'failed') item.failed += 1
      map.set(task.project_id, item)
    }
    return map
  }, [tasks])

  return (
    <div>
      <div className="page-head">
        <div>
          <h2 className="page-title">任务中心</h2>
          <p className="page-sub">按项目查看任务；资产出图等不属于项目的任务在最下方单独列出，可整批取消。</p>
        </div>
      </div>
      <ProjectCards
        projects={projects}
        loading={loading}
        onOpen={(id) => navigate(`/tasks/${id}`)}
        emptyText="还没有项目，先去「我的项目」创建一部短剧"
        stats={(project) => {
          const item = counts.get(project.id)
          return [
            { label: '任务', value: item?.total ?? 0 },
            { label: '进行中', value: item?.active ?? 0 },
            { label: '失败', value: item?.failed ?? 0 },
          ]
        }}
      />
      {orphanStats.total > 0 && (
        <Row gutter={[16, 16]} style={{ marginTop: 16 }}>
          <Col xs={24} sm={12} lg={8} xxl={6}>
            <Card
              className="hover-card"
              hoverable
              style={{ cursor: 'pointer' }}
              onClick={() => navigate('/tasks/orphan')}
              title={
                <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  <InboxOutlined style={{ color: '#f59f00' }} />
                  <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: 150 }}>
                    未关联项目的任务
                  </span>
                </span>
              }
              extra={
                <Space size={8}>
                  <Tag color={orphanStats.active ? 'processing' : 'default'}>
                    {orphanStats.active ? `${orphanStats.active} 进行中` : '空闲'}
                  </Tag>
                  <span className="muted" style={{ fontSize: 12.5 }}>
                    查看 <RightOutlined style={{ fontSize: 10 }} />
                  </span>
                </Space>
              }
            >
              <Typography.Paragraph
                type="secondary"
                ellipsis={{ rows: 2 }}
                style={{ minHeight: 44, marginBottom: 10 }}
              >
                资产出图等不归属任何项目的任务，点开可查看进度并整批取消。
              </Typography.Paragraph>
              <div className="muted" style={{ display: 'flex', gap: 14, flexWrap: 'wrap', fontSize: 12.5 }}>
                <span>任务 {orphanStats.total}</span>
                <span>进行中 {orphanStats.active}</span>
                <span>失败 {orphanStats.failed}</span>
              </div>
            </Card>
          </Col>
        </Row>
      )}
    </div>
  )
}

/** 二级页：某个项目的任务，按分镜分组。 */
function ProjectTasks({ projectId }: { projectId: number }) {
  const { message } = App.useApp()
  const navigate = useNavigate()
  const [project, setProject] = useState<Project | null>(null)
  const [shots, setShots] = useState<Record<number, ShotInfo>>({})
  const [tasks, setTasks] = useState<GenerationTask[]>([])
  const [loading, setLoading] = useState(true)
  const [kind, setKind] = useState<string | undefined>()
  const [status, setStatus] = useState<string | undefined>()
  const [detailId, setDetailId] = useState<number | null>(null)
  const [selected, setSelected] = useState<number[]>([])

  // 失败诊断 agent：读已有结论或新起一条诊断任务；默认只分析，不重跑
  const [diagnoseTask, setDiagnoseTask] = useState<GenerationTask | null>(null)
  const [diagnosis, setDiagnosis] = useState<Diagnosis | null>(null)
  const [diagnosisLoading, setDiagnosisLoading] = useState(false)
  const [diagnoseBusy, setDiagnoseBusy] = useState(false)
  const [allowRetry, setAllowRetry] = useState(false)

  // AI 处理记录：循环 agent 与单次审查器的结论、轨迹都从 agent_runs 只读汇总
  const [agentRunsOpen, setAgentRunsOpen] = useState(false)
  const [agentRunsLoading, setAgentRunsLoading] = useState(false)
  const [agentRuns, setAgentRuns] = useState<AgentRun[]>([])
  const [agentRunKind, setAgentRunKind] = useState<string | undefined>()

  const load = useCallback(async () => {
    try {
      const list = await taskApi.list({ project_id: projectId, kind, status })
      setTasks(list)
      // 筛选或刷新后，已不在列表里的勾选要一并剔除，避免批量删除命中看不见的任务
      setSelected((prev) => prev.filter((id) => list.some((task) => task.id === id)))
    } catch (error) {
      message.error(describeError(error))
    } finally {
      setLoading(false)
    }
  }, [projectId, kind, status, message])

  // 分镜信息：用于把任务按分镜分组
  const loadStructure = useCallback(async () => {
    try {
      const [projectInfo, episodes] = await Promise.all([
        projectApi.get(projectId),
        projectApi.listEpisodes(projectId),
      ])
      const ordered = [...episodes].sort((a, b) => a.order_index - b.order_index)
      const shotLists = await Promise.all(ordered.map((episode) => episodeApi.listShots(episode.id)))
      const map: Record<number, ShotInfo> = {}
      ordered.forEach((episode, index) => {
        shotLists[index].forEach((shot) => {
          map[shot.id] = {
            episode: episode.title,
            order: shot.order_index,
            title: shot.title,
            sort: `${String(episode.order_index).padStart(3, '0')}-${String(shot.order_index).padStart(3, '0')}`,
          }
        })
      })
      setProject(projectInfo)
      setShots(map)
    } catch (error) {
      message.error(describeError(error))
    }
  }, [projectId, message])

  useEffect(() => {
    setLoading(true)
    void loadStructure()
  }, [loadStructure])

  useEffect(() => {
    void load()
  }, [load])

  const hasActive = useMemo(() => tasks.some((task) => ACTIVE.has(task.status)), [tasks])
  const activeTotal = useMemo(
    () => tasks.filter((task) => ACTIVE.has(task.status)).length,
    [tasks],
  )
  /** 可删除的任务数（已结束的），用于「全部删除」按钮的计数与显隐 */
  const completedTotal = useMemo(
    () => tasks.filter((task) => !ACTIVE.has(task.status)).length,
    [tasks],
  )

  useEffect(() => {
    if (!hasActive) return
    const timer = window.setInterval(() => void load(), 2500)
    return () => window.clearInterval(timer)
  }, [hasActive, load])

  // 详情抽屉：始终取列表里的最新快照，运行中的任务会随轮询自动刷新
  const detail = useMemo(() => tasks.find((task) => task.id === detailId) ?? null, [tasks, detailId])

  const openDiagnosis = async (record: GenerationTask) => {
    setDiagnoseTask(record)
    setDiagnosis(null)
    setAllowRetry(false)
    setDiagnosisLoading(true)
    try {
      setDiagnosis(await taskApi.diagnosis(record.id))
    } catch (error) {
      message.error(describeError(error))
    } finally {
      setDiagnosisLoading(false)
    }
  }

  const submitDiagnosis = async () => {
    if (!diagnoseTask) return
    setDiagnoseBusy(true)
    try {
      await taskApi.diagnose(diagnoseTask.id, { allow_retry: allowRetry })
      message.success(
        allowRetry
          ? '诊断 agent 已启动：允许换一套凭证重跑一次'
          : '诊断 agent 已启动：只分析原因，不重跑',
      )
      setDiagnoseTask(null)
      await load()
    } catch (error) {
      message.error(describeError(error))
    } finally {
      setDiagnoseBusy(false)
    }
  }

  const diagnosisReport = diagnosis?.summary?.diagnosis ?? null

  const openAgentRuns = async () => {
    setAgentRunsOpen(true)
    setAgentRunsLoading(true)
    try {
      setAgentRuns(await agentRunApi.list({ project_id: projectId, kind: agentRunKind, limit: 120 }))
    } catch (error) {
      message.error(describeError(error))
    } finally {
      setAgentRunsLoading(false)
    }
  }

  const filterAgentRuns = async (kind?: string) => {
    setAgentRunKind(kind)
    setAgentRunsLoading(true)
    try {
      setAgentRuns(await agentRunApi.list({ project_id: projectId, kind, limit: 120 }))
    } catch (error) {
      message.error(describeError(error))
    } finally {
      setAgentRunsLoading(false)
    }
  }

  const handleCancel = async (id: number) => {
    try {
      await taskApi.cancel(id)
      message.success('已请求取消')
      await load()
    } catch (error) {
      message.error(describeError(error))
    }
  }

  const handleCancelAll = async () => {
    try {
      const result = await taskApi.cancelActive({ project_id: projectId })
      message.success(result.canceled ? `已取消 ${result.canceled} 个进行中任务` : '没有进行中的任务')
      await load()
    } catch (error) {
      message.error(describeError(error))
    }
  }

  const handleRetry = async (id: number) => {
    try {
      await taskApi.retry(id)
      message.success('已重新提交')
      await load()
    } catch (error) {
      message.error(describeError(error))
    }
  }

  const handleDelete = async (id: number) => {
    try {
      await taskApi.remove(id)
      message.success('任务记录已删除')
      if (detailId === id) setDetailId(null)
      setTasks((prev) => prev.filter((task) => task.id !== id))
      setSelected((prev) => prev.filter((selectedId) => selectedId !== id))
    } catch (error) {
      message.error(describeError(error))
    }
  }

  const confirmDelete = (record: GenerationTask) => (
    <Popconfirm
      title="删除该任务记录？"
      description="仅删除记录，已生成的素材不受影响。"
      okText="删除"
      cancelText="取消"
      okButtonProps={{ danger: true }}
      onConfirm={() => handleDelete(record.id)}
    >
      <Button size="small" danger type="text" icon={<DeleteOutlined />} />
    </Popconfirm>
  )

  const handleBatchDelete = async () => {
    try {
      const result = await taskApi.removeBatch(selected)
      setSelected([])
      if (result.deleted) message.success(`已删除 ${result.deleted} 条任务记录`)
      if (result.skipped_ids.length) {
        message.warning(`${result.skipped_ids.length} 条任务仍在进行中，已跳过，请先取消再删除`)
      }
      await load()
    } catch (error) {
      message.error(describeError(error))
    }
  }

  const handleDeleteAll = async (params: {
    project_id?: number
    orphan?: boolean
    kind?: string
    status?: string
  }) => {
    try {
      const result = await taskApi.removeAll(params)
      setSelected([])
      if (result.deleted) message.success(`已删除 ${result.deleted} 条任务记录`)
      else message.info('没有可删除的已结束任务')
      if (result.skipped_ids.length) {
        message.warning(`${result.skipped_ids.length} 条任务仍在进行中，未删除，请先取消`)
      }
      await load()
    } catch (error) {
      message.error(describeError(error))
    }
  }

  /** 每个分组表格各自维护勾选，但共用一个 selected 数组，跨组选择不会丢 */
  const rowSelectionFor = (rows: GenerationTask[]): TableRowSelection<GenerationTask> => ({
    selectedRowKeys: selected.filter((id) => rows.some((row) => row.id === id)),
    onChange: (keys) => {
      const rowIds = new Set(rows.map((row) => row.id))
      setSelected((prev) => [...prev.filter((id) => !rowIds.has(id)), ...keys.map(Number)])
    },
    // 进行中的任务禁止勾选：后端也会跳过，这里提前挡住避免用户白点
    getCheckboxProps: (record) => ({ disabled: ACTIVE.has(record.status) }),
  })

  // 表内不再重复显示「项目」（已由页面头交代），聚焦分镜内的任务本身
  const columns: ColumnsType<GenerationTask> = [
    { title: 'ID', dataIndex: 'id', width: 60 },
    {
      title: '类型',
      dataIndex: 'kind',
      width: 80,
      render: (value: string) => <Tag>{KIND_LABEL[value] ?? value}</Tag>,
    },
    {
      title: '模型',
      dataIndex: 'model',
      width: 180,
      ellipsis: true,
      responsive: ['xl'],
      render: (value: string | null, record) => (
        <Typography.Text style={{ fontSize: 12.5 }}>
          {value || '—'}
          {record.provider ? ` · ${record.provider}` : ''}
        </Typography.Text>
      ),
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 140,
      render: (value: string, record) => {
        const meta = STATUS[value] ?? { text: value, color: 'default' }
        return (
          <Space size={8}>
            <Tag color={meta.color}>{meta.text}</Tag>
            {ACTIVE.has(value) && <Progress percent={record.progress} size="small" style={{ width: 64 }} />}
          </Space>
        )
      },
    },
    {
      title: '结果 / 错误',
      dataIndex: 'error',
      width: 180,
      ellipsis: true,
      responsive: ['md'],
      render: (error: string | null, record) => {
        if (error) return <Typography.Text type="danger" style={{ fontSize: 12 }}>{error}</Typography.Text>
        if (record.result?.url) return <Typography.Text type="secondary" style={{ fontSize: 12 }}>已产出素材</Typography.Text>
        return <Typography.Text type="secondary">—</Typography.Text>
      },
    },
    {
      title: '操作',
      key: 'actions',
      width: 250,
      render: (_, record) => (
        <Space size={4}>
          <Button size="small" onClick={() => setDetailId(record.id)}>
            查看
          </Button>
          {ACTIVE.has(record.status) && (
            <Button size="small" icon={<StopOutlined />} onClick={() => handleCancel(record.id)}>
              取消
            </Button>
          )}
          {record.status === 'failed' && !['diagnose', 'repair', 'cut_repair'].includes(record.kind) && (
            <Button size="small" icon={<RobotOutlined />} onClick={() => void openDiagnosis(record)}>
              诊断
            </Button>
          )}
          {!ACTIVE.has(record.status) && (
            <>
              <Button size="small" icon={<RedoOutlined />} onClick={() => handleRetry(record.id)}>
                重试
              </Button>
              {confirmDelete(record)}
            </>
          )}
        </Space>
      ),
    },
  ]

  // 按分镜分组；项目级任务（剧本 / 拆解 / 导出，无 shot_id）单独一组
  const groups = useMemo(() => {
    const buckets = new Map<string, { key: string; label: string; sort: string; tasks: GenerationTask[] }>()
    for (const task of tasks) {
      const info = task.shot_id != null ? shots[task.shot_id] : undefined
      const key = info ? `shot-${task.shot_id}` : 'unlinked'
      if (!buckets.has(key)) {
        buckets.set(key, {
          key,
          label: info
            ? `${info.episode} · 镜头 ${info.order}${info.title ? ` · ${info.title}` : ''}`
            : '项目级任务（剧本 / 拆解 / 导出）',
          sort: info ? info.sort : 'zzz',
          tasks: [],
        })
      }
      buckets.get(key)!.tasks.push(task)
    }
    return [...buckets.values()].sort((a, b) => (a.sort < b.sort ? -1 : 1))
  }, [tasks, shots])

  return (
    <div>
      <div className="page-head">
        <div>
          <Space size={10} align="center" style={{ marginBottom: 2 }}>
            <Button icon={<ArrowLeftOutlined />} size="small" onClick={() => navigate('/tasks')}>
              返回
            </Button>
            <h2 className="page-title" style={{ margin: 0 }}>
              {project?.title ?? '项目'} · 任务中心
            </h2>
          </Space>
          <p className="page-sub">
            共 {tasks.length} 个任务，按分镜分组；进行中会自动刷新。
            {activeTotal > 0 && ` 当前 ${activeTotal} 个进行中。`}
          </p>
        </div>
        <Space wrap>
          <Select
            allowClear
            placeholder="类型"
            style={{ width: 110 }}
            value={kind}
            onChange={setKind}
            options={Object.entries(KIND_LABEL).map(([value, label]) => ({ value, label }))}
          />
          <Select
            allowClear
            placeholder="状态"
            style={{ width: 120 }}
            value={status}
            onChange={setStatus}
            options={Object.entries(STATUS).map(([value, meta]) => ({ value, label: meta.text }))}
          />
          <Button icon={<RobotOutlined />} onClick={() => void openAgentRuns()}>
            AI 处理记录
          </Button>
          {selected.length > 0 && (
            <Popconfirm
              title={`删除选中的 ${selected.length} 条任务记录？`}
              description="仅删除记录，已生成的素材仍保留在素材库。"
              okText="删除"
              cancelText="取消"
              okButtonProps={{ danger: true }}
              onConfirm={handleBatchDelete}
            >
              <Button danger icon={<DeleteOutlined />}>
                批量删除 {selected.length}
              </Button>
            </Popconfirm>
          )}
          {completedTotal > 0 && (
            <Popconfirm
              title={`删除本项目全部 ${completedTotal} 条已结束任务记录？`}
              description="进行中的任务不会被删除；已生成的素材仍保留在素材库。"
              okText="全部删除"
              cancelText="取消"
              okButtonProps={{ danger: true }}
              onConfirm={() => handleDeleteAll({ project_id: projectId, kind, status })}
            >
              <Button danger type="text" icon={<DeleteOutlined />}>
                全部删除
              </Button>
            </Popconfirm>
          )}
          {activeTotal > 0 && (
            <Popconfirm
              title={`取消本项目全部 ${activeTotal} 个进行中的任务？`}
              description="取消后该项目的后续链式任务会停止推进，已生成的部分会保留。"
              okText="全部取消"
              cancelText="再想想"
              onConfirm={handleCancelAll}
            >
              <Button danger icon={<StopOutlined />}>
                全部取消
              </Button>
            </Popconfirm>
          )}
          <Button icon={<ReloadOutlined />} onClick={() => void load()}>
            刷新
          </Button>
        </Space>
      </div>

      {loading ? null : groups.length === 0 ? (
        <Card>
          <Empty description="该项目暂无任务记录" />
        </Card>
      ) : (
        groups.map((group) => {
          const activeCount = group.tasks.filter((task) => ACTIVE.has(task.status)).length
          const failedCount = group.tasks.filter((task) => task.status === 'failed').length
          return (
            <Card
              key={group.key}
              style={{ marginBottom: 16 }}
              styles={{ body: { padding: 0 } }}
              title={group.label}
              extra={
                <Typography.Text type="secondary" style={{ fontSize: 12.5 }}>
                  {group.tasks.length} 个任务
                  {activeCount > 0 ? ` · ${activeCount} 进行中` : ''}
                  {failedCount > 0 ? ` · ${failedCount} 失败` : ''}
                </Typography.Text>
              }
            >
              <Table<GenerationTask>
                rowKey="id"
                size="small"
                columns={columns}
                dataSource={group.tasks}
                rowSelection={rowSelectionFor(group.tasks)}
                pagination={{ pageSize: 8, hideOnSinglePage: true }}
                scroll={{ x: 'max-content' }}
              />
            </Card>
          )
        })
      )}

      {/* AI 处理记录：5 个循环 agent + 2 个单次审查器的统一审计视图 */}
      <Drawer
        title="AI 处理记录（本项目）"
        open={agentRunsOpen}
        onClose={() => setAgentRunsOpen(false)}
        width="min(640px, 94vw)"
        extra={<Button onClick={() => void openAgentRuns()}>刷新</Button>}
      >
        <Space direction="vertical" size={10} style={{ width: '100%' }}>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            自检、诊断、修复、整理与两类审查的结论、步骤、工具轨迹都留在这里；只读，不影响任务与素材。
          </Typography.Text>
          <Select
            allowClear
            size="small"
            placeholder="按类型筛选"
            style={{ width: 200 }}
            value={agentRunKind}
            onChange={(value) => void filterAgentRuns(value)}
            options={Object.entries(AGENT_KIND_LABEL).map(([value, label]) => ({ value, label }))}
          />
          {agentRunsLoading ? (
            <Typography.Text type="secondary">正在读取…</Typography.Text>
          ) : agentRuns.length === 0 ? (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="本项目还没有 agent 运行记录" />
          ) : (
            agentRuns.map((run) => (
              <Card key={run.id} size="small" styles={{ body: { padding: 10 } }}>
                <Space size={6} wrap>
                  <Tag color="blue">{AGENT_KIND_LABEL[run.kind] ?? run.kind}</Tag>
                  <Tag color={AGENT_STATUS_COLOR[run.status ?? ''] ?? 'default'}>
                    {AGENT_STATUS_LABEL[run.status ?? ''] ?? run.status ?? '—'}
                  </Tag>
                  <Tag>
                    {run.target_type ?? '—'} {run.target_id ?? '—'}
                  </Tag>
                  <Tag>步骤 {run.steps}</Tag>
                  {run.model && <Tag>{run.model}</Tag>}
                  <Typography.Text type="secondary" style={{ fontSize: 11.5 }}>
                    {formatTime(run.created_at)}
                  </Typography.Text>
                </Space>
                {run.error && (
                  <Typography.Paragraph type="danger" style={{ fontSize: 12, margin: '6px 0 0' }}>
                    {run.error}
                  </Typography.Paragraph>
                )}
                {run.summary && (
                  <pre className="task-json" style={{ maxHeight: 160, marginTop: 6 }}>
                    {JSON.stringify(run.summary, null, 2)}
                  </pre>
                )}
                {run.trace.length > 0 && (
                  <details style={{ marginTop: 6 }}>
                    <summary style={{ fontSize: 12, cursor: 'pointer' }}>
                      工具轨迹（{run.trace.length} 步）
                    </summary>
                    {run.trace.map((step, index) => (
                      <Typography.Paragraph
                        key={`${step.step}-${index}`}
                        style={{ fontSize: 12, margin: '4px 0 0' }}
                      >
                        <Tag color={step.ok ? 'blue' : 'red'}>{step.action}</Tag>
                        {step.observation}
                      </Typography.Paragraph>
                    ))}
                  </details>
                )}
              </Card>
            ))
          )}
        </Space>
      </Drawer>

      {/* 失败诊断 agent：读结论或新起任务；默认只分析，重跑需显式勾选 */}
      <Modal
        title={diagnoseTask ? `AI 诊断 · 任务 #${diagnoseTask.id}` : 'AI 诊断'}
        open={!!diagnoseTask}
        onCancel={() => setDiagnoseTask(null)}
        width={640}
        destroyOnClose
        footer={
          <Space>
            <Button onClick={() => setDiagnoseTask(null)}>关闭</Button>
            <Button
              type="primary"
              icon={<RobotOutlined />}
              loading={diagnoseBusy}
              onClick={() => void submitDiagnosis()}
            >
              {diagnosis?.found ? '重新诊断' : '开始诊断'}
            </Button>
          </Space>
        }
      >
        {diagnoseTask && (
          <Space direction="vertical" size={12} style={{ width: '100%' }}>
            <Alert
              type="error"
              showIcon
              message="失败原因"
              description={diagnoseTask.error || '未记录错误'}
            />
            {diagnosisLoading ? (
              <Typography.Text type="secondary">正在读取诊断记录…</Typography.Text>
            ) : diagnosis?.found ? (
              <>
                <Space size={6} wrap>
                  <Tag
                    color={(DIAGNOSIS_STATUS[diagnosis.status ?? ''] ?? { color: 'default' }).color}
                  >
                    {(DIAGNOSIS_STATUS[diagnosis.status ?? ''] ?? { text: diagnosis.status }).text}
                  </Tag>
                  {diagnosisReport && (
                    <Tag color="blue">
                      {diagnosisReport.category_label || diagnosisReport.category}
                    </Tag>
                  )}
                  {diagnosisReport && (
                    <Tag>{CONFIDENCE_LABEL[diagnosisReport.confidence] ?? diagnosisReport.confidence}</Tag>
                  )}
                  {diagnosis.model && <Tag>{diagnosis.model}</Tag>}
                  <Tag>步骤 {diagnosis.steps ?? 0}</Tag>
                </Space>
                {diagnosisReport?.suggestion && (
                  <Alert
                    type="info"
                    showIcon
                    message="建议"
                    description={diagnosisReport.suggestion}
                  />
                )}
                {diagnosisReport?.evidence?.length ? (
                  <div>
                    <Typography.Text strong style={{ fontSize: 13 }}>
                      判断依据
                    </Typography.Text>
                    {diagnosisReport.evidence.map((item, index) => (
                      <Typography.Paragraph key={index} style={{ fontSize: 12.5, margin: '4px 0 0' }}>
                        · {item}
                      </Typography.Paragraph>
                    ))}
                  </div>
                ) : null}
                {diagnosis.summary?.retry_task_id && (
                  <Alert
                    type="warning"
                    showIcon
                    message={`已创建独立重跑任务 #${diagnosis.summary.retry_task_id}（不推进原链）`}
                  />
                )}
                {diagnosis.error && (
                  <Alert type="error" showIcon message="诊断执行失败" description={diagnosis.error} />
                )}
                {(diagnosis.trace ?? []).length > 0 && (
                  <div>
                    <Typography.Text strong style={{ fontSize: 13 }}>
                      工具轨迹
                    </Typography.Text>
                    {diagnosis.trace!.map((step, index) => (
                      <Card
                        key={`${step.step}-${index}`}
                        size="small"
                        style={{ marginTop: 6 }}
                        styles={{ body: { padding: 8 } }}
                      >
                        <Space size={6} wrap style={{ marginBottom: 4 }}>
                          <Tag>第 {step.step} 步</Tag>
                          <Tag color={step.ok ? 'blue' : 'red'}>{step.action}</Tag>
                        </Space>
                        {step.thought ? (
                          <Typography.Paragraph style={{ fontSize: 12.5, marginBottom: 4 }}>
                            判断：{step.thought}
                          </Typography.Paragraph>
                        ) : null}
                        <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginBottom: 0 }}>
                          观察：{step.observation}
                        </Typography.Paragraph>
                      </Card>
                    ))}
                  </div>
                )}
              </>
            ) : (
              <Alert
                type="info"
                showIcon
                message="还没有诊断记录"
                description="启动后 agent 会读取任务参数、镜头与凭证状态，给出归因与处理建议；默认不重跑。"
              />
            )}
            <Checkbox checked={allowRetry} onChange={(event) => setAllowRetry(event.target.checked)}>
              允许 agent 换一套凭证重跑一次（会产生费用；新任务是独立任务，不推进原链）
            </Checkbox>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              诊断只给建议，不会修改原任务、镜头状态或已生成素材。
            </Typography.Text>
          </Space>
        )}
      </Modal>

      <TaskDetailDrawer task={detail} onClose={() => setDetailId(null)} />
    </div>
  )
}

/** 任务详情抽屉：项目二级页与未关联任务页共用，避免两套详情逻辑各差一半。 */
function TaskDetailDrawer({
  task,
  onClose,
}: {
  task: GenerationTask | null
  onClose: () => void
}) {
  return (
    <Drawer
      title={task ? `任务 #${task.id} · ${KIND_LABEL[task.kind] ?? task.kind}` : '任务详情'}
      open={!!task}
      onClose={onClose}
      closable
      extra={<Button onClick={onClose}>关闭</Button>}
      footer={
        <div style={{ textAlign: 'right' }}>
          <Button type="primary" onClick={onClose}>
            关闭
          </Button>
        </div>
      }
      width="min(560px, 92vw)"
      destroyOnClose
    >
      {task && (
        <Space direction="vertical" size={16} style={{ width: '100%' }}>
          <Descriptions column={1} size="small" bordered>
            <Descriptions.Item label="状态">
              <Space size={8}>
                <Tag color={(STATUS[task.status] ?? { color: 'default' }).color}>
                  {(STATUS[task.status] ?? { text: task.status }).text}
                </Tag>
                {ACTIVE.has(task.status) && <Progress percent={task.progress} size="small" style={{ width: 120 }} />}
              </Space>
            </Descriptions.Item>
            <Descriptions.Item label="厂商 / 模型">
              {task.provider || '—'} · {task.model || '—'}
            </Descriptions.Item>
            <Descriptions.Item label="关联">
              项目 {task.project_id ?? '—'} · 分集 {task.episode_id ?? '—'} · 分镜 {task.shot_id ?? '—'}
            </Descriptions.Item>
            <Descriptions.Item label="创建 / 开始 / 结束">
              {formatTime(task.created_at)} / {formatTime(task.started_at)} / {formatTime(task.finished_at)}
            </Descriptions.Item>
          </Descriptions>

          {task.error && (
            <Alert type="error" showIcon message="失败原因" description={task.error} />
          )}

          {task.kind === 'diagnose' && task.result?.diagnosis && (
            <Alert
              type="info"
              showIcon
              message={`AI 诊断：${task.result.diagnosis.category_label ?? task.result.diagnosis.category ?? '未知'}`}
              description={task.result.diagnosis.suggestion}
            />
          )}

          {task.kind === 'cut_repair' && task.result?.summary && (
            <Alert type="info" showIcon message="切点修复结论" description={task.result.summary} />
          )}

          {task.result?.url ? (
            <div>
              <Typography.Text strong style={{ fontSize: 13 }}>
                产出结果
              </Typography.Text>
              <div style={{ marginTop: 8 }}>
                {task.kind === 'video' || task.kind === 'export' ? (
                  <video
                    src={mediaUrl(task.result.url as string)}
                    controls
                    preload="metadata"
                    style={{ width: '100%', maxHeight: '52vh', background: '#000', borderRadius: 8 }}
                  />
                ) : (
                  <img
                    src={mediaUrl(task.result.url as string)}
                    alt="任务结果"
                    style={{ width: '100%', borderRadius: 8 }}
                  />
                )}
              </div>
              <Typography.Link href={task.result.url as string} target="_blank" style={{ fontSize: 12 }}>
                在新标签打开原始文件
              </Typography.Link>
              {Array.isArray(task.result.fallbacks) && task.result.fallbacks.length > 0 && (
                <Alert
                  style={{ marginTop: 8 }}
                  type="warning"
                  showIcon
                  message="曾触发候选兜底"
                  description={
                    <Typography.Text style={{ fontSize: 12 }}>
                      {(task.result.fallbacks as { provider: string; error: string }[])
                        .map((item) => `${item.provider}：${item.error}`)
                        .join('；')}
                    </Typography.Text>
                  }
                />
              )}
            </div>
          ) : (
            !task.error && (
              <Alert
                type={ACTIVE.has(task.status) ? 'info' : 'warning'}
                showIcon
                message={ACTIVE.has(task.status) ? '任务执行中，暂无产出' : '该任务没有产出文件'}
                description={
                  task.result
                    ? '该任务产出为文本内容，结构化结果见下方「请求参数 / 返回结果」。'
                    : undefined
                }
              />
            )
          )}

          <div>
            <Typography.Text strong style={{ fontSize: 13 }}>
              请求参数 / 返回结果
            </Typography.Text>
            <pre className="task-json">{JSON.stringify({ params: task.params, result: task.result }, null, 2)}</pre>
          </div>
        </Space>
      )}
    </Drawer>
  )
}

/** 二级页：未关联项目的任务（如全局资产出图），可单独取消或整批停止。 */
function OrphanTasks() {
  const { message } = App.useApp()
  const navigate = useNavigate()
  const [tasks, setTasks] = useState<GenerationTask[]>([])
  const [loading, setLoading] = useState(true)
  const [detailId, setDetailId] = useState<number | null>(null)
  const [selected, setSelected] = useState<number[]>([])

  const load = useCallback(async () => {
    try {
      const list = await taskApi.list({ orphan: true })
      setTasks(list)
      // 3 秒轮询会把已结束的任务状态改掉，勾选按 id 保留但剔除已不存在的
      setSelected((prev) => prev.filter((id) => list.some((task) => task.id === id)))
    } catch (error) {
      message.error(describeError(error))
    } finally {
      setLoading(false)
    }
  }, [message])

  useEffect(() => {
    void load()
  }, [load])

  const activeTotal = useMemo(
    () => tasks.filter((task) => ACTIVE.has(task.status)).length,
    [tasks],
  )
  const completedTotal = useMemo(
    () => tasks.filter((task) => !ACTIVE.has(task.status)).length,
    [tasks],
  )

  useEffect(() => {
    if (!activeTotal) return
    const timer = window.setInterval(() => void load(), 3000)
    return () => window.clearInterval(timer)
  }, [activeTotal, load])

  const handleCancel = async (id: number) => {
    try {
      await taskApi.cancel(id)
      message.success('已请求取消')
      await load()
    } catch (error) {
      message.error(describeError(error))
    }
  }

  const handleCancelAll = async () => {
    try {
      const result = await taskApi.cancelActive({ orphan: true })
      message.success(result.canceled ? `已取消 ${result.canceled} 个任务` : '没有进行中的任务')
      await load()
    } catch (error) {
      message.error(describeError(error))
    }
  }

  const handleRetry = async (id: number) => {
    try {
      await taskApi.retry(id)
      message.success('已重新提交')
      await load()
    } catch (error) {
      message.error(describeError(error))
    }
  }

  const handleDelete = async (id: number) => {
    try {
      await taskApi.remove(id)
      message.success('任务记录已删除')
      if (detailId === id) setDetailId(null)
      setTasks((prev) => prev.filter((task) => task.id !== id))
      setSelected((prev) => prev.filter((selectedId) => selectedId !== id))
    } catch (error) {
      message.error(describeError(error))
    }
  }

  const confirmDelete = (record: GenerationTask) => (
    <Popconfirm
      title="删除该任务记录？"
      description="仅删除记录，已生成的素材不受影响。"
      okText="删除"
      cancelText="取消"
      okButtonProps={{ danger: true }}
      onConfirm={() => handleDelete(record.id)}
    >
      <Button size="small" danger type="text" icon={<DeleteOutlined />} />
    </Popconfirm>
  )

  const handleBatchDelete = async () => {
    try {
      const result = await taskApi.removeBatch(selected)
      setSelected([])
      if (result.deleted) message.success(`已删除 ${result.deleted} 条任务记录`)
      if (result.skipped_ids.length) {
        message.warning(`${result.skipped_ids.length} 条任务仍在进行中，已跳过，请先取消再删除`)
      }
      await load()
    } catch (error) {
      message.error(describeError(error))
    }
  }

  const handleDeleteAll = async (params: {
    project_id?: number
    orphan?: boolean
    kind?: string
    status?: string
  }) => {
    try {
      const result = await taskApi.removeAll(params)
      setSelected([])
      if (result.deleted) message.success(`已删除 ${result.deleted} 条任务记录`)
      else message.info('没有可删除的已结束任务')
      if (result.skipped_ids.length) {
        message.warning(`${result.skipped_ids.length} 条任务仍在进行中，未删除，请先取消`)
      }
      await load()
    } catch (error) {
      message.error(describeError(error))
    }
  }

  /** 每个分组表格各自维护勾选，但共用一个 selected 数组，跨组选择不会丢 */
  const rowSelectionFor = (rows: GenerationTask[]): TableRowSelection<GenerationTask> => ({
    selectedRowKeys: selected.filter((id) => rows.some((row) => row.id === id)),
    onChange: (keys) => {
      const rowIds = new Set(rows.map((row) => row.id))
      setSelected((prev) => [...prev.filter((id) => !rowIds.has(id)), ...keys.map(Number)])
    },
    // 进行中的任务禁止勾选：后端也会跳过，这里提前挡住避免用户白点
    getCheckboxProps: (record) => ({ disabled: ACTIVE.has(record.status) }),
  })

  const detail = useMemo(() => tasks.find((task) => task.id === detailId) ?? null, [tasks, detailId])

  const columns: ColumnsType<GenerationTask> = [
    { title: 'ID', dataIndex: 'id', width: 64 },
    {
      title: '类型',
      dataIndex: 'kind',
      width: 76,
      render: (value: string) => <Tag>{KIND_LABEL[value] ?? value}</Tag>,
    },
    {
      title: '来源',
      key: 'source',
      render: (_, record) => (
        <Typography.Text style={{ fontSize: 12.5 }}>
          {record.params?.asset_id ? `资产 #${record.params.asset_id} 出图` : '独立任务'}
        </Typography.Text>
      ),
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 160,
      render: (value: string, record) => {
        const meta = STATUS[value] ?? { text: value, color: 'default' }
        return (
          <Space size={8}>
            <Tag color={meta.color}>{meta.text}</Tag>
            {ACTIVE.has(value) && (
              <Progress percent={record.progress} size="small" style={{ width: 56 }} />
            )}
          </Space>
        )
      },
    },
    { title: '创建时间', dataIndex: 'created_at', width: 150, render: (value: string) => formatTime(value) },
    {
      title: '操作',
      key: 'actions',
      width: 190,
      render: (_, record) => (
        <Space size={4}>
          <Button size="small" onClick={() => setDetailId(record.id)}>
            查看
          </Button>
          {ACTIVE.has(record.status) && (
            <Button size="small" icon={<StopOutlined />} onClick={() => void handleCancel(record.id)}>
              取消
            </Button>
          )}
          {!ACTIVE.has(record.status) && (
            <>
              <Button size="small" icon={<RedoOutlined />} onClick={() => void handleRetry(record.id)}>
                重试
              </Button>
              {confirmDelete(record)}
            </>
          )}
        </Space>
      ),
    },
  ]

  return (
    <div>
      <div className="page-head">
        <div>
          <Space size={10} align="center" style={{ marginBottom: 2 }}>
            <Button icon={<ArrowLeftOutlined />} size="small" onClick={() => navigate('/tasks')}>
              返回
            </Button>
            <h2 className="page-title" style={{ margin: 0 }}>
              未关联项目的任务
            </h2>
          </Space>
          <p className="page-sub">
            共 {tasks.length} 个任务{activeTotal > 0 ? `，当前 ${activeTotal} 个进行中` : ''}；每 3 秒自动刷新。
          </p>
        </div>
        <Space wrap>
          {selected.length > 0 && (
            <Popconfirm
              title={`删除选中的 ${selected.length} 条任务记录？`}
              description="仅删除记录，已生成的素材仍保留在素材库。"
              okText="删除"
              cancelText="取消"
              okButtonProps={{ danger: true }}
              onConfirm={handleBatchDelete}
            >
              <Button danger icon={<DeleteOutlined />}>
                批量删除 {selected.length}
              </Button>
            </Popconfirm>
          )}
          {completedTotal > 0 && (
            <Popconfirm
              title={`删除全部 ${completedTotal} 条已结束任务记录？`}
              description="进行中的任务不会被删除；已生成的素材仍保留在素材库。"
              okText="全部删除"
              cancelText="取消"
              okButtonProps={{ danger: true }}
              onConfirm={() => handleDeleteAll({ orphan: true })}
            >
              <Button danger type="text" icon={<DeleteOutlined />}>
                全部删除
              </Button>
            </Popconfirm>
          )}
          {activeTotal > 0 && (
            <Popconfirm
              title={`取消全部 ${activeTotal} 个进行中的任务？`}
              description="已提交给厂商的请求会尽量撤销，已扣费的部分无法退回。"
              okText="全部取消"
              cancelText="再想想"
              onConfirm={handleCancelAll}
            >
              <Button danger icon={<StopOutlined />}>
                全部取消
              </Button>
            </Popconfirm>
          )}
          <Button icon={<ReloadOutlined />} onClick={() => void load()}>
            刷新
          </Button>
        </Space>
      </div>

      {!loading && tasks.length === 0 ? (
        <Card>
          <Empty description="没有未关联项目的任务" />
        </Card>
      ) : (
        <Card styles={{ body: { padding: 0 } }}>
          <Table
            rowKey="id"
            loading={loading}
            columns={columns}
            dataSource={tasks}
            rowSelection={rowSelectionFor(tasks)}
            pagination={{ pageSize: 20, hideOnSinglePage: true }}
            scroll={{ x: 720 }}
          />
        </Card>
      )}

      <TaskDetailDrawer task={detail} onClose={() => setDetailId(null)} />
    </div>
  )
}

export default function TasksPage() {
  const { projectId } = useParams<{ projectId?: string }>()
  if (projectId === 'orphan') return <OrphanTasks />
  const parsed = Number(projectId)
  return projectId && Number.isFinite(parsed) ? <ProjectTasks projectId={parsed} /> : <TasksProjects />
}
