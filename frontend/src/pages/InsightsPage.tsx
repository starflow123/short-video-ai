import { useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Card,
  Checkbox,
  Col,
  Descriptions,
  Drawer,
  Empty,
  Row,
  Select,
  Space,
  Statistic,
  Table,
  Tabs,
  Tag,
  Typography,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'

import { describeError } from '@/api/client'
import { evalApi, knowledgeApi, projectApi, statsApi } from '@/api/endpoints'
import type {
  EvalRunDetail,
  EvalRunItem,
  EvalRunSummary,
  KnowledgeNote,
  Project,
  QualityStats,
} from '@/api/types'

const VERDICT: Record<string, { text: string; color: string }> = {
  pass: { text: '通过', color: 'green' },
  warn: { text: '需注意', color: 'gold' },
  fail: { text: '不合格', color: 'red' },
  error: { text: '审查失败', color: 'default' },
}

const NOTE_STATUS: Record<string, { text: string; color: string }> = {
  active: { text: '生效', color: 'green' },
  stale: { text: '待复核', color: 'orange' },
  archived: { text: '已归档', color: 'default' },
}

const NOTE_CATEGORY: Record<string, string> = {
  fact: '事实',
  heuristic: '经验',
  style: '画风',
}

const EVAL_KIND: Record<string, string> = {
  golden_shot: '复审现有主帧',
  golden_generate: '按当前编译器重新出图',
}

function fmt(value?: string | null) {
  return value ? new Date(value).toLocaleString() : '—'
}

function verdictTag(verdict?: string | null) {
  if (!verdict) return <Tag>未审查</Tag>
  const meta = VERDICT[verdict] ?? { text: verdict, color: 'default' }
  return <Tag color={meta.color}>{meta.text}</Tag>
}

/** 台账是旁路事实：失败不算进平均耗时，认可状态实时关联关键帧。 */
function StatsPanel({ projectId }: { projectId?: number }) {
  const [days, setDays] = useState(7)
  const [data, setData] = useState<QualityStats | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let alive = true
    setError(null)
    statsApi
      .generations({ project_id: projectId, days })
      .then((value) => alive && setData(value))
      .catch((err) => alive && setError(describeError(err)))
    return () => {
      alive = false
    }
  }, [days, projectId])

  const providerRows = useMemo(
    () =>
      Object.entries(data?.by_provider ?? {}).map(([provider, value]) => ({
        key: provider,
        provider,
        ...value,
        rate: value.total ? Math.round((value.succeeded / value.total) * 100) : 0,
      })),
    [data],
  )
  const dayRows = useMemo(
    () =>
      Object.entries(data?.by_day ?? {}).map(([day, value]) => ({
        key: day,
        day,
        ...value,
        rate: value.total ? Math.round((value.succeeded / value.total) * 100) : 0,
      })),
    [data],
  )

  const providerColumns: ColumnsType<(typeof providerRows)[number]> = [
    { title: '厂商', dataIndex: 'provider' },
    { title: '任务数', dataIndex: 'total', width: 90 },
    { title: '成功', dataIndex: 'succeeded', width: 80 },
    { title: '失败', dataIndex: 'failed', width: 80 },
    { title: '成功率', dataIndex: 'rate', width: 90, render: (value: number) => `${value}%` },
  ]

  const dayColumns: ColumnsType<(typeof dayRows)[number]> = [
    { title: '日期', dataIndex: 'day' },
    { title: '任务数', dataIndex: 'total', width: 90 },
    { title: '成功', dataIndex: 'succeeded', width: 80 },
    { title: '成功率', dataIndex: 'rate', width: 90, render: (value: number) => `${value}%` },
  ]

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Space wrap>
        <Select
          value={days}
          style={{ width: 120 }}
          onChange={setDays}
          options={[
            { value: 7, label: '最近 7 天' },
            { value: 14, label: '最近 14 天' },
            { value: 30, label: '最近 30 天' },
          ]}
        />
        <Typography.Text type="secondary">
          平均耗时只统计成功任务（失败耗时取决于重试策略）。
        </Typography.Text>
      </Space>
      {error && <Alert type="error" showIcon message={error} />}
      <Row gutter={[16, 16]}>
        <Col xs={12} lg={6}>
          <Card>
            <Statistic title="生成任务" value={data?.total ?? 0} suffix="条" />
          </Card>
        </Col>
        <Col xs={12} lg={6}>
          <Card>
            <Statistic
              title="成功率"
              value={data?.success_rate == null ? '—' : Math.round(data.success_rate * 100)}
              suffix={data?.success_rate == null ? undefined : '%'}
            />
          </Card>
        </Col>
        <Col xs={12} lg={6}>
          <Card>
            <Statistic
              title="平均耗时"
              value={data?.avg_duration_sec ?? '—'}
              suffix={data?.avg_duration_sec == null ? undefined : '秒'}
            />
          </Card>
        </Col>
        <Col xs={12} lg={6}>
          <Card>
            <Statistic title="人工已认可" value={data?.human_approved ?? 0} suffix="条" />
          </Card>
        </Col>
      </Row>
      <Row gutter={[16, 16]}>
        <Col xs={24} xl={12}>
          <Card title="按厂商" size="small">
            <Table
              size="small"
              rowKey="key"
              pagination={false}
              columns={providerColumns}
              dataSource={providerRows}
              locale={{ emptyText: '窗口内没有生成任务' }}
            />
          </Card>
        </Col>
        <Col xs={24} xl={12}>
          <Card title="按天" size="small">
            <Table
              size="small"
              rowKey="key"
              pagination={false}
              columns={dayColumns}
              dataSource={dayRows}
              locale={{ emptyText: '窗口内没有生成任务' }}
            />
          </Card>
        </Col>
      </Row>
      <Card title="质量维度（实时关联）" size="small">
        <Space wrap>
          {Object.entries(data?.review_verdicts ?? {}).length === 0 ? (
            <Typography.Text type="secondary">暂无 AI 视觉初审记录</Typography.Text>
          ) : (
            Object.entries(data?.review_verdicts ?? {}).map(([verdict, count]) => (
              <Tag key={verdict} color={VERDICT[verdict]?.color ?? 'default'}>
                {VERDICT[verdict]?.text ?? verdict} {count}
              </Tag>
            ))
          )}
        </Space>
      </Card>
    </Space>
  )
}

function EvalPanel({ projectId }: { projectId?: number }) {
  const [runs, setRuns] = useState<EvalRunSummary[]>([])
  const [error, setError] = useState<string | null>(null)
  const [detail, setDetail] = useState<EvalRunDetail | null>(null)
  const [detailError, setDetailError] = useState<string | null>(null)
  const [open, setOpen] = useState(false)

  useEffect(() => {
    let alive = true
    setError(null)
    if (!projectId) {
      setRuns([])
      return
    }
    evalApi
      .list({ project_id: projectId, limit: 30 })
      .then((value) => alive && setRuns(value))
      .catch((err) => alive && setError(describeError(err)))
    return () => {
      alive = false
    }
  }, [projectId])

  const openDetail = (runId: number) => {
    setOpen(true)
    setDetail(null)
    setDetailError(null)
    evalApi
      .get(runId)
      .then(setDetail)
      .catch((err) => setDetailError(describeError(err)))
  }

  const columns: ColumnsType<EvalRunSummary> = [
    { title: '#', dataIndex: 'id', width: 70 },
    {
      title: '模式',
      dataIndex: 'kind',
      width: 170,
      render: (kind: string) => EVAL_KIND[kind] ?? kind,
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 100,
      render: (status: string) => (
        <Tag color={status === 'completed' ? 'green' : status === 'failed' ? 'red' : 'blue'}>
          {status}
        </Tag>
      ),
    },
    {
      title: '结论',
      dataIndex: 'totals',
      render: (totals?: Record<string, number> | null) =>
        totals ? (
          <Space size={4} wrap>
            <Tag color="green">通过 {totals.pass ?? 0}</Tag>
            <Tag color="gold">需注意 {totals.warn ?? 0}</Tag>
            <Tag color="red">不合格 {totals.fail ?? 0}</Tag>
            {totals.error ? <Tag>错误 {totals.error}</Tag> : null}
          </Space>
        ) : (
          '—'
        ),
    },
    {
      title: '编译器指纹',
      dataIndex: 'prompt_version',
      width: 130,
      render: (value?: string | null) => (value ? <code>{value}</code> : '—'),
    },
    { title: '时间', dataIndex: 'created_at', width: 180, render: fmt },
  ]

  const itemColumns: ColumnsType<EvalRunItem> = [
    { title: '镜头', dataIndex: 'shot_id', width: 70 },
    {
      title: '结论',
      dataIndex: 'verdict',
      width: 100,
      render: (value?: string | null) => verdictTag(value),
    },
    {
      title: '与上次相比',
      key: 'changed',
      width: 140,
      render: (_, item) => {
        if (!item.prev_verdict) return <Typography.Text type="secondary">首次</Typography.Text>
        if (!item.changed) return <Typography.Text type="secondary">无变化</Typography.Text>
        return (
          <Space size={4}>
            {verdictTag(item.prev_verdict)}
            <span>→</span>
            {verdictTag(item.verdict)}
          </Space>
        )
      },
    },
    {
      title: '原因',
      dataIndex: 'reasons',
      render: (value: unknown) =>
        Array.isArray(value) ? value.join('；') : value ? JSON.stringify(value) : '—',
    },
    { title: '错误', dataIndex: 'error', render: (value?: string | null) => value || '—' },
  ]

  if (!projectId) {
    return <Empty description="先在上方选择项目，再查看该项目的金镜头回归记录" />
  }

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Alert
        type="info"
        showIcon
        message="回归只产报告，不改变任何镜头的认可状态；重新出图模式会消耗供应商额度。"
      />
      {error && <Alert type="error" showIcon message={error} />}
      <Table
        rowKey="id"
        size="small"
        pagination={false}
        columns={columns}
        dataSource={runs}
        locale={{ emptyText: '还没有回归记录' }}
        onRow={(record) => ({
          onClick: () => openDetail(record.id),
          style: { cursor: 'pointer' },
        })}
      />
      <Drawer
        width={760}
        open={open}
        onClose={() => setOpen(false)}
        title={detail ? `金镜头回归 #${detail.id}` : '金镜头回归'}
      >
        {detailError && <Alert type="error" showIcon message={detailError} />}
        {detail && (
          <Space direction="vertical" size={16} style={{ width: '100%' }}>
            <Descriptions size="small" column={2} bordered>
              <Descriptions.Item label="项目">{detail.project_id}</Descriptions.Item>
              <Descriptions.Item label="模式">
                {EVAL_KIND[detail.kind] ?? detail.kind}
              </Descriptions.Item>
              <Descriptions.Item label="状态">{detail.status}</Descriptions.Item>
              <Descriptions.Item label="编译器指纹">
                {detail.prompt_version ?? '—'}
              </Descriptions.Item>
              <Descriptions.Item label="汇总" span={2}>
                {detail.totals
                  ? `通过 ${detail.totals.pass ?? 0}；需注意 ${detail.totals.warn ?? 0}；` +
                    `不合格 ${detail.totals.fail ?? 0}；错误 ${detail.totals.error ?? 0}`
                  : '—'}
              </Descriptions.Item>
              <Descriptions.Item label="时间" span={2}>
                {fmt(detail.created_at)}
              </Descriptions.Item>
            </Descriptions>
            <Table
              rowKey="shot_id"
              size="small"
              pagination={false}
              columns={itemColumns}
              dataSource={detail.items}
              expandable={{
                expandedRowRender: (item) => (
                  <pre style={{ margin: 0, whiteSpace: 'pre-wrap' }}>
                    {JSON.stringify(item.checks ?? {}, null, 2)}
                  </pre>
                ),
              }}
            />
          </Space>
        )}
      </Drawer>
    </Space>
  )
}

function KnowledgePanel({
  projectId,
  projects,
}: {
  projectId?: number
  projects: Project[]
}) {
  const [includeUnapproved, setIncludeUnapproved] = useState(false)
  const [status, setStatus] = useState<string | undefined>()
  const [category, setCategory] = useState<string | undefined>()
  const [notes, setNotes] = useState<KnowledgeNote[]>([])
  const [injection, setInjection] = useState<KnowledgeNote[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  const project = useMemo(
    () => projects.find((item) => item.id === projectId),
    [projectId, projects],
  )
  const styleContext = useMemo(
    () =>
      [project?.visual_style, project?.style].filter(Boolean).join(' ') || undefined,
    [project],
  )

  useEffect(() => {
    let alive = true
    setError(null)
    knowledgeApi
      .list({ include_unapproved: includeUnapproved, status, category })
      .then((value) => alive && setNotes(value))
      .catch((err) => alive && setError(describeError(err)))
    return () => {
      alive = false
    }
  }, [includeUnapproved, status, category])

  useEffect(() => {
    let alive = true
    setInjection(null)
    if (!projectId) return
    knowledgeApi
      .lookup({ project_id: projectId, visual_style: styleContext, media_type: 'image' })
      .then((value) => alive && setInjection(value))
      .catch((err) => alive && setError(describeError(err)))
    return () => {
      alive = false
    }
  }, [projectId, styleContext])

  const columns: ColumnsType<KnowledgeNote> = [
    {
      title: '标题',
      dataIndex: 'title',
      render: (title: string) => <Typography.Text strong>{title}</Typography.Text>,
    },
    {
      title: '分类',
      dataIndex: 'category',
      width: 80,
      render: (value: string) => NOTE_CATEGORY[value] ?? value,
    },
    {
      title: '范围',
      dataIndex: 'scope',
      width: 90,
      render: (value: string, note) =>
        value === 'global' ? '全局' : `项目 ${note.project_id ?? '?'}`,
    },
    {
      title: '画风桶',
      dataIndex: 'visual_style',
      width: 110,
      render: (value?: string | null) => value || <Typography.Text type="secondary">不限</Typography.Text>,
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 90,
      render: (value: string) => {
        const meta = NOTE_STATUS[value] ?? { text: value, color: 'default' }
        return <Tag color={meta.color}>{meta.text}</Tag>
      },
    },
    {
      title: '认可',
      dataIndex: 'approved',
      width: 80,
      render: (value: boolean) =>
        value ? <Tag color="green">已认可</Tag> : <Tag color="orange">待认可</Tag>,
    },
    { title: '最近使用', dataIndex: 'last_used_at', width: 170, render: fmt },
    {
      title: '取代链',
      dataIndex: 'superseded_by',
      width: 90,
      render: (value?: number | null) => (value ? `→ #${value}` : '—'),
    },
  ]

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Alert
        type="info"
        showIcon
        message="AI 只能进待认可队列；只有「已认可 + 生效 + 范围/前置条件/画风桶都匹配」的条目才会被注入修复 agent。"
      />
      <Space wrap>
        <Checkbox
          checked={includeUnapproved}
          onChange={(event) => setIncludeUnapproved(event.target.checked)}
        >
          含待认可（AI 提案）
        </Checkbox>
        <Select
          allowClear
          placeholder="全部状态"
          style={{ width: 140 }}
          value={status}
          onChange={setStatus}
          options={[
            { value: 'active', label: '生效' },
            { value: 'stale', label: '待复核' },
            { value: 'archived', label: '已归档' },
          ]}
        />
        <Select
          allowClear
          placeholder="全部分类"
          style={{ width: 140 }}
          value={category}
          onChange={setCategory}
          options={[
            { value: 'fact', label: '事实' },
            { value: 'heuristic', label: '经验' },
            { value: 'style', label: '画风' },
          ]}
        />
      </Space>
      {error && <Alert type="error" showIcon message={error} />}
      <Row gutter={[16, 16]}>
        <Col xs={24} xl={15}>
          <Table
            rowKey="id"
            size="small"
            pagination={{ pageSize: 10 }}
            columns={columns}
            dataSource={notes}
            locale={{ emptyText: '还没有条目' }}
            expandable={{
              expandedRowRender: (note) => (
                <Space direction="vertical" size={4}>
                  <Typography.Text>{note.content}</Typography.Text>
                  <Typography.Text type="secondary">
                    前置条件：{note.preconditions ? JSON.stringify(note.preconditions) : '无'}
                  </Typography.Text>
                  <Typography.Text type="secondary">
                    创建：{fmt(note.created_at)}；最近确认：{fmt(note.last_confirmed_at)}
                  </Typography.Text>
                </Space>
              ),
            }}
          />
        </Col>
        <Col xs={24} xl={9}>
          <Card
            size="small"
            title={project ? `注入预览：${project.title}` : '注入预览'}
            extra={<Typography.Text type="secondary">实际注入结果</Typography.Text>}
          >
            {!projectId ? (
              <Typography.Text type="secondary">在上方选择项目后，这里展示修复 agent 实际会拿到的条目。</Typography.Text>
            ) : injection == null ? (
              <Typography.Text type="secondary">加载中…</Typography.Text>
            ) : injection.length === 0 ? (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description="没有条目通过资格门（认可 / 生效 / 范围 / 前置条件 / 画风桶）"
              />
            ) : (
              <Space direction="vertical" size={8} style={{ width: '100%' }}>
                {injection.map((note) => (
                  <Card key={note.id} size="small" type="inner" title={note.title}>
                    <Typography.Paragraph style={{ marginBottom: 0 }}>
                      {note.content}
                    </Typography.Paragraph>
                  </Card>
                ))}
              </Space>
            )}
            {projectId && styleContext && (
              <Typography.Paragraph type="secondary" style={{ marginTop: 12, marginBottom: 0 }}>
                画风上下文：{styleContext}
              </Typography.Paragraph>
            )}
          </Card>
        </Col>
      </Row>
    </Space>
  )
}

export default function InsightsPage() {
  const [projects, setProjects] = useState<Project[]>([])
  const [projectId, setProjectId] = useState<number | undefined>()
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    projectApi
      .list()
      .then(setProjects)
      .catch((err) => setError(describeError(err)))
  }, [])

  return (
    <div>
      <div className="page-head">
        <div>
          <h2 className="page-title">质量看板</h2>
          <p className="page-sub">
            台账、金镜头回归与生产教训库；只读展示，认可 / 回滚仍走各自的人工入口。
          </p>
        </div>
        <Space wrap>
          <Select
            allowClear
            showSearch
            optionFilterProp="label"
            placeholder="全部项目"
            style={{ width: 260 }}
            value={projectId}
            onChange={setProjectId}
            options={projects.map((item) => ({ value: item.id, label: item.title }))}
          />
        </Space>
      </div>
      {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 12 }} />}
      <Tabs
        items={[
          { key: 'stats', label: '生成质量', children: <StatsPanel projectId={projectId} /> },
          { key: 'eval', label: '金镜头回归', children: <EvalPanel projectId={projectId} /> },
          {
            key: 'knowledge',
            label: '生产教训库',
            children: <KnowledgePanel projectId={projectId} projects={projects} />,
          },
        ]}
      />
    </div>
  )
}
