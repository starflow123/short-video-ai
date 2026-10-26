import {
  App,
  Alert,
  Button,
  Card,
  Col,
  Empty,
  Form,
  Input,
  InputNumber,
  Progress,
  Row,
  Select,
  Space,
  Tag,
  Tooltip,
  Typography,
  Upload,
} from 'antd'
import type { UploadProps } from 'antd'
import {
  DeleteOutlined,
  DownloadOutlined,
  FileOutlined,
  FolderOpenOutlined,
  InboxOutlined,
  ScissorOutlined,
} from '@ant-design/icons'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { describeError } from '@/api/client'
import { editApi, projectApi, taskApi } from '@/api/endpoints'
import type { EditResult, GenerationTask, MediaAsset, Project } from '@/api/types'

interface Clip {
  mediaId: number
  name: string
  duration: number
  size?: number | null
  note: string
}

const ACTIVE = new Set(['pending', 'running'])

function formatDuration(seconds?: number | null): string {
  if (!seconds || seconds <= 0) return '—'
  const total = Math.round(seconds)
  const minute = Math.floor(total / 60)
  const second = total % 60
  return minute > 0 ? `${minute}分${second}秒` : `${second}秒`
}

function formatSize(bytes?: number | null): string {
  if (!bytes) return '—'
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

export default function SmartEditPage() {
  const { message } = App.useApp()
  const navigate = useNavigate()
  const [clips, setClips] = useState<Clip[]>([])
  const [projects, setProjects] = useState<Project[]>([])
  const [projectId, setProjectId] = useState<number | null>(null)
  const [history, setHistory] = useState<GenerationTask[]>([])
  const [uploading, setUploading] = useState(0)
  const [uploadPercent, setUploadPercent] = useState(0)
  const [target, setTarget] = useState(30)
  const [aspect, setAspect] = useState('9:16')
  const [pace, setPace] = useState<'fast' | 'medium' | 'slow'>('medium')
  const [instruction, setInstruction] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [task, setTask] = useState<GenerationTask | null>(null)
  const [result, setResult] = useState<EditResult | null>(null)
  const timer = useRef<number | null>(null)

  const loadHistory = useCallback(async () => {
    try {
      setHistory(await taskApi.list({ kind: 'edit', limit: 8 }))
    } catch {
      // 历史加载失败不影响当前剪辑，任务中心仍可查看
    }
  }, [])

  useEffect(() => {
    void (async () => {
      try {
        setProjects(await projectApi.list())
      } catch {
        // 没有项目时仍可剪辑，成片进「未归属素材」
      }
      void loadHistory()
    })()
  }, [loadHistory])

  const totalDuration = useMemo(
    () => clips.reduce((sum, clip) => sum + (clip.duration || 0), 0),
    [clips],
  )

  const uploadProps: UploadProps = {
    multiple: true,
    accept: 'video/*,.mp4,.mov,.m4v,.webm,.mkv,.avi',
    showUploadList: false,
    customRequest: async ({ file, onSuccess, onError }) => {
      const raw = file as File
      setUploading((value) => value + 1)
      setUploadPercent(0)
      try {
        const asset: MediaAsset = await editApi.upload(raw, setUploadPercent)
        setClips((list) => [
          ...list,
          {
            mediaId: asset.id,
            name: asset.filename,
            duration: asset.duration_seconds || 0,
            size: asset.size_bytes,
            note: '',
          },
        ])
        onSuccess?.(asset)
        message.success(`已上传 ${asset.filename}`)
      } catch (error) {
        onError?.(error as Error)
        message.error(describeError(error))
      } finally {
        setUploading((value) => Math.max(0, value - 1))
      }
    },
  }

  const removeClip = (mediaId: number) =>
    setClips((list) => list.filter((clip) => clip.mediaId !== mediaId))

  const patchNote = (mediaId: number, note: string) =>
    setClips((list) => list.map((clip) => (clip.mediaId === mediaId ? { ...clip, note } : clip)))

  const stopPolling = useCallback(() => {
    if (timer.current) {
      window.clearTimeout(timer.current)
      timer.current = null
    }
  }, [])

  useEffect(() => stopPolling, [stopPolling])

  const poll = useCallback(
    async (id: number) => {
      try {
        const next = await taskApi.get(id)
        setTask(next)
        if (next.status === 'succeeded') {
          setResult((next.result || null) as EditResult | null)
          message.success('成片已生成，可在下方「最近剪辑」或素材库查看')
          void loadHistory()
          return
        }
        if (next.status === 'failed' || next.status === 'canceled') {
          message.error(
            next.error || (next.status === 'canceled' ? '剪辑已取消' : '剪辑失败'),
          )
          return
        }
      } catch {
        // 单次轮询失败继续，任务中心仍可查看最终状态
      }
      timer.current = window.setTimeout(() => void poll(id), 2500)
    },
    [message, loadHistory],
  )

  const start = async () => {
    if (!clips.length) {
      message.warning('先上传至少一段视频素材')
      return
    }
    setSubmitting(true)
    setResult(null)
    stopPolling()
    try {
      const created = await editApi.create({
        clips: clips.map((clip) => ({
          media_id: clip.mediaId,
          note: clip.note.trim() || null,
        })),
        target_duration: target,
        aspect_ratio: aspect,
        pace,
        instruction: instruction.trim() || null,
        project_id: projectId,
      })
      setTask(created)
      message.success('剪辑任务已提交，正在分析素材')
      void poll(created.id)
    } catch (error) {
      message.error(describeError(error))
    } finally {
      setSubmitting(false)
    }
  }

  const running = task ? ACTIVE.has(task.status) : false
  const progress = result ? 100 : task?.progress ?? 0
  const resultProject =
    result?.project_id != null ? projects.find((project) => project.id === result.project_id) : undefined

  return (
    <div>
      <div className="page-head">
        <div>
          <h2 className="page-title">
            <ScissorOutlined style={{ marginRight: 8 }} />
            智能剪辑
          </h2>
          <p className="page-sub">
            上传多段素材，AI 按你的时长与要求挑片段、排顺序，自动裁切拼接成一条成片。
          </p>
        </div>
      </div>

      <Row gutter={[16, 16]}>
        <Col xs={24} lg={14} xxl={15}>
          <Card title="1. 上传素材" className="edit-card">
            <Upload.Dragger {...uploadProps} disabled={running}>
              <p className="ant-upload-drag-icon">
                <InboxOutlined />
              </p>
              <p className="ant-upload-text">点击或拖入视频文件</p>
              <p className="ant-upload-hint">
                支持 mp4 / mov / webm / mkv，单个不超过 300MB；可一次选多个。
              </p>
            </Upload.Dragger>

            {uploading > 0 && (
              <div style={{ marginTop: 12 }}>
                <Typography.Text type="secondary" style={{ fontSize: 12.5 }}>
                  正在上传 {uploading} 个文件（{uploadPercent}%）
                </Typography.Text>
                <Progress percent={uploadPercent} size="small" showInfo={false} />
              </div>
            )}

            <div style={{ marginTop: 16 }}>
              <Space style={{ marginBottom: 8 }} wrap>
                <Typography.Text strong>已选素材 {clips.length} 段</Typography.Text>
                <Tag>总时长 {formatDuration(totalDuration)}</Tag>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  备注可以写「打斗高潮」「空镜转场」「主角特写」，AI 选段会参考它。
                </Typography.Text>
              </Space>

              {clips.length === 0 ? (
                <Empty description="还没有素材" image={Empty.PRESENTED_IMAGE_SIMPLE} />
              ) : (
                <Space direction="vertical" size={10} style={{ width: '100%' }}>
                  {clips.map((clip, index) => (
                    <Card key={clip.mediaId} size="small" styles={{ body: { padding: 12 } }}>
                      <Row gutter={12} align="middle">
                        <Col flex="none">
                          <div className="clip-badge">
                            <FileOutlined />
                          </div>
                        </Col>
                        <Col flex="auto" style={{ minWidth: 0 }}>
                          <Typography.Text
                            ellipsis
                            style={{ display: 'block', fontSize: 13, maxWidth: 320 }}
                          >
                            {index + 1}. {clip.name}
                          </Typography.Text>
                          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                            {formatDuration(clip.duration)} · {formatSize(clip.size)}
                          </Typography.Text>
                          <Input
                            size="small"
                            placeholder="这段素材的备注（可选）"
                            value={clip.note}
                            maxLength={200}
                            disabled={running}
                            onChange={(event) => patchNote(clip.mediaId, event.target.value)}
                            style={{ marginTop: 6 }}
                          />
                        </Col>
                        <Col flex="none">
                          <Tooltip title="移除这段素材">
                            <Button
                              type="text"
                              danger
                              icon={<DeleteOutlined />}
                              disabled={running}
                              onClick={() => removeClip(clip.mediaId)}
                            />
                          </Tooltip>
                        </Col>
                      </Row>
                    </Card>
                  ))}
                </Space>
              )}
            </div>
          </Card>
        </Col>

        <Col xs={24} lg={10} xxl={9}>
          <Card title="2. 剪辑设置" className="edit-card">
            <Form layout="vertical" requiredMark={false}>
              <Form.Item label="目标成片时长（秒）" extra="5-300 秒，AI 会按这个时长凑片段">
                <InputNumber
                  min={5}
                  max={300}
                  value={target}
                  disabled={running}
                  onChange={(value) => setTarget(Number(value) || 30)}
                  style={{ width: '100%' }}
                />
              </Form.Item>
              <Row gutter={12}>
                <Col span={12}>
                  <Form.Item label="画幅">
                    <Select
                      value={aspect}
                      disabled={running}
                      onChange={setAspect}
                      options={[
                        { value: '9:16', label: '9:16 竖屏' },
                        { value: '16:9', label: '16:9 横屏' },
                        { value: '1:1', label: '1:1 方图' },
                        { value: '4:3', label: '4:3' },
                        { value: '3:4', label: '3:4' },
                      ]}
                    />
                  </Form.Item>
                </Col>
                <Col span={12}>
                  <Form.Item label="节奏">
                    <Select
                      value={pace}
                      disabled={running}
                      onChange={setPace}
                      options={[
                        { value: 'fast', label: '快节奏（短片段）' },
                        { value: 'medium', label: '中等' },
                        { value: 'slow', label: '慢节奏（长镜头）' },
                      ]}
                    />
                  </Form.Item>
                </Col>
              </Row>
              <Form.Item
                label="成片归属项目（可选）"
                extra="不选则进「素材库 → 未归属项目的素材」，同样能找回与下载"
              >
                <Select
                  allowClear
                  placeholder="不归属任何项目"
                  value={projectId ?? undefined}
                  disabled={running}
                  onChange={(value) => setProjectId(value ?? null)}
                  options={projects.map((project) => ({
                    value: project.id,
                    label: project.title,
                  }))}
                />
              </Form.Item>
              <Form.Item label="剪辑要求（可选）">
                <Input.TextArea
                  rows={4}
                  maxLength={500}
                  showCount
                  value={instruction}
                  disabled={running}
                  onChange={(event) => setInstruction(event.target.value)}
                  placeholder="例如：先交代环境，再进入打斗，结尾停在主角表情；不要出现上一条素材的开头"
                />
              </Form.Item>
            </Form>

            <Alert
              type="info"
              showIcon
              style={{ marginBottom: 12 }}
              message="只剪画面，不加字幕与配音"
              description="系统按场景切分和你的备注选段；无音轨素材会自动补静音。成片存入素材库，归属所选项目；没选项目则在「未归属项目的素材」里。"
            />

            <Button
              type="primary"
              size="large"
              block
              loading={submitting}
              disabled={running || clips.length === 0}
              icon={<ScissorOutlined />}
              onClick={() => void start()}
            >
              开始智能剪辑
            </Button>

            {task && (
              <div style={{ marginTop: 16 }}>
                <Space style={{ marginBottom: 6 }} wrap>
                  <Tag color={running ? 'processing' : task.status === 'succeeded' ? 'success' : 'error'}>
                    任务 #{task.id}
                  </Tag>
                  <Typography.Text type="secondary" style={{ fontSize: 12.5 }}>
                    {running ? '分析/合成中' : task.status === 'succeeded' ? '已完成' : task.status}
                  </Typography.Text>
                </Space>
                <Progress percent={progress} size="small" status={running ? 'active' : undefined} />
                {task.error && (
                  <Typography.Text type="danger" style={{ fontSize: 12.5 }}>
                    {task.error}
                  </Typography.Text>
                )}
              </div>
            )}
          </Card>

          {result && (
            <Card
              title="成片"
              style={{ marginTop: 16 }}
              extra={
                <Space>
                  <Tag color="success">{formatDuration(result.duration_seconds)}</Tag>
                  <Button
                    size="small"
                    icon={<FolderOpenOutlined />}
                    onClick={() =>
                      navigate(result.project_id != null ? `/media/${result.project_id}` : '/media/unlinked')
                    }
                  >
                    素材库
                  </Button>
                  <Button
                    size="small"
                    icon={<DownloadOutlined />}
                    href={result.url}
                    target="_blank"
                  >
                    下载
                  </Button>
                </Space>
              }
            >
              <video
                src={result.url}
                controls
                playsInline
                style={{ width: '100%', borderRadius: 8, background: '#000' }}
              />
              {result.title && (
                <Typography.Text strong style={{ display: 'block', marginTop: 10 }}>
                  {result.title}
                </Typography.Text>
              )}
              <Typography.Text type="secondary" style={{ fontSize: 12.5 }}>
                共 {result.segments} 个片段 · {result.aspect_ratio} ·{' '}
                {resultProject
                  ? `已存入「${resultProject.title}」的素材库`
                  : '已存入素材库 → 未归属项目的素材'}
              </Typography.Text>
            </Card>
          )}

          <Card
            title="最近剪辑"
            style={{ marginTop: 16 }}
            extra={
              <Button size="small" onClick={() => void loadHistory()}>
                刷新
              </Button>
            }
          >
            {history.length === 0 ? (
              <Empty description="还没有剪辑记录" image={Empty.PRESENTED_IMAGE_SIMPLE} />
            ) : (
              <Space direction="vertical" size={8} style={{ width: '100%' }}>
                {history.map((item) => {
                  const res = (item.result || null) as EditResult | null
                  const active = ACTIVE.has(item.status)
                  return (
                    <Card key={item.id} size="small" styles={{ body: { padding: 10 } }}>
                      <Space size={8} wrap>
                        <Tag color={active ? 'processing' : item.status === 'succeeded' ? 'success' : 'error'}>
                          #{item.id}
                        </Tag>
                        <Typography.Text style={{ fontSize: 12.5 }}>
                          {res?.title || (item.params?.instruction as string) || '智能剪辑'}
                        </Typography.Text>
                        {res ? <Tag>{formatDuration(res.duration_seconds)}</Tag> : null}
                        {active ? <Tag color="processing">{item.progress}%</Tag> : null}
                        {res?.url ? (
                          <Button size="small" type="link" href={res.url} target="_blank">
                            打开成片
                          </Button>
                        ) : null}
                      </Space>
                      <Typography.Text type="secondary" style={{ fontSize: 11.5, display: 'block', marginTop: 4 }}>
                        {new Date(item.created_at).toLocaleString()} ·{' '}
                        {item.project_id != null ? '归属项目' : '未归属项目'} ·{' '}
                        {item.status === 'succeeded'
                          ? '已完成'
                          : item.status === 'failed'
                            ? item.error || '失败'
                            : item.status}
                      </Typography.Text>
                    </Card>
                  )
                })}
              </Space>
            )}
          </Card>
        </Col>
      </Row>
    </div>
  )
}
