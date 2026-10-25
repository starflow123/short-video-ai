import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  App,
  AutoComplete,
  Button,
  Card,
  Col,
  Empty,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Progress,
  Row,
  Select,
  Skeleton,
  Switch,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import {
  DeleteOutlined,
  EditOutlined,
  ImportOutlined,
  PlusOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'

import { projectApi } from '@/api/endpoints'
import { describeError } from '@/api/client'
import { filterStyleOption, STYLE_OPTIONS } from '@/constants/styles'
import type { Project, ProjectPayload } from '@/api/types'

const ASPECTS = [
  { value: '9:16', label: '9:16 竖屏短剧' },
  { value: '16:9', label: '16:9 横屏' },
  { value: '1:1', label: '1:1 方形' },
  { value: '4:3', label: '4:3 复古' },
]

/** 视觉风格：对应「画面是哪种画法」 */
const VISUAL_STYLES = [
  { value: '现实', label: '现实（写实摄影）' },
  { value: '日式动漫', label: '日式动漫' },
  { value: '国风水墨', label: '国风水墨' },
  { value: '油画质感', label: '油画质感' },
  { value: '赛博朋克', label: '赛博朋克' },
  { value: '3D 动画', label: '3D 动画' },
  { value: '复古胶片', label: '复古胶片' },
  { value: '卡通', label: '卡通' },
]

/** 视频风格：对应「题材赛道」，用作分类标签 */
const VIDEO_STYLES = [
  { value: '真人都市', label: '真人都市' },
  { value: '古装仙侠', label: '古装仙侠' },
  { value: '现代校园', label: '现代校园' },
  { value: '悬疑惊悚', label: '悬疑惊悚' },
  { value: '甜宠爱情', label: '甜宠爱情' },
  { value: '科幻未来', label: '科幻未来' },
  { value: '历史正剧', label: '历史正剧' },
  { value: '武侠江湖', label: '武侠江湖' },
]

const STATUS_LABEL: Record<string, { text: string; color: string }> = {
  draft: { text: '草稿', color: 'default' },
  storyboarded: { text: '已分镜', color: 'blue' },
  producing: { text: '生成中', color: 'processing' },
  completed: { text: '已完成', color: 'success' },
}

/** 根据项目状态/进度推出「当前阶段」文案 */
function stageOf(p: Project): { title: string; hint: string; color: string } {
  if (p.shot_count === 0) {
    return { title: '待准备剧本', hint: '还没有分镜，先在详情页写剧本或一键生成', color: 'orange' }
  }
  if (p.shots_running > 0) {
    return { title: '生成中', hint: `${p.shots_running} 个镜头正在出图/出视频`, color: 'processing' }
  }
  if (p.progress >= 100) {
    return { title: '已完成', hint: '所有镜头关键帧与视频就绪，可导出成片', color: 'success' }
  }
  if (p.shots_pending > 0) {
    return { title: '待准备镜头', hint: `共 ${p.shot_count} 个分镜，建议先进分镜工作室补全提示词`, color: 'blue' }
  }
  return { title: '待生成画面', hint: '关键帧已就绪，可开始生成视频', color: 'cyan' }
}

export default function ProjectsPage() {
  const { message } = App.useApp()
  const navigate = useNavigate()
  const [loading, setLoading] = useState(true)
  const [projects, setProjects] = useState<Project[]>([])
  const [open, setOpen] = useState(false)
  const [editing, setEditing] = useState<Project | null>(null)
  const [saving, setSaving] = useState(false)
  const [idea, setIdea] = useState('')
  const [suggesting, setSuggesting] = useState(false)
  const [form] = Form.useForm<ProjectPayload>()

  const load = useCallback(async () => {
    setLoading(true)
    try {
      setProjects(await projectApi.list())
    } catch (error) {
      message.error(describeError(error))
    } finally {
      setLoading(false)
    }
  }, [message])

  useEffect(() => {
    void load()
  }, [load])

  const resetForm = () => {
    form.resetFields()
    setIdea('')
  }

  const openCreate = () => {
    setEditing(null)
    resetForm()
    setOpen(true)
  }

  const openEdit = (p: Project) => {
    setEditing(p)
    form.setFieldsValue({
      title: p.title,
      description: p.description,
      aspect_ratio: p.aspect_ratio,
      style: p.style,
      visual_style: p.visual_style,
      video_style: p.video_style,
      seed: p.seed,
      style_lock: p.style_lock,
    })
    setOpen(true)
  }

  const handleSubmit = async () => {
    const values = await form.validateFields()
    setSaving(true)
    try {
      if (editing) {
        const updated = await projectApi.update(editing.id, values)
        message.success('项目已保存')
        setProjects((prev) => prev.map((it) => (it.id === updated.id ? updated : it)))
      } else {
        const project = await projectApi.create(values)
        message.success('项目已创建')
        navigate(`/projects/${project.id}`)
        return
      }
    } catch (error) {
      message.error(describeError(error))
      return
    } finally {
      setSaving(false)
    }
    setOpen(false)
    resetForm()
  }

  const handleSuggest = async () => {
    const text = idea.trim()
    if (!text) {
      message.warning('先写一句想法，AI 才能帮你补全')
      return
    }
    const aspectRatio = (form.getFieldValue('aspect_ratio') as string) || '9:16'
    setSuggesting(true)
    try {
      const suggestion = await projectApi.suggest({ idea: text, aspect_ratio: aspectRatio })
      form.setFieldsValue({
        title: suggestion.title,
        description: suggestion.description,
        visual_style: suggestion.visual_style || undefined,
        video_style: suggestion.video_style || undefined,
        style: suggestion.style,
        aspect_ratio: suggestion.aspect_ratio || aspectRatio,
      })
      message.success(
        suggestion.tags.length
          ? `已生成设定 · 题材标签：${suggestion.tags.join(' / ')}`
          : '已生成项目设定，可继续微调',
      )
    } catch (error) {
      message.error(describeError(error))
    } finally {
      setSuggesting(false)
    }
  }

  const handleDelete = async (id: number) => {
    try {
      await projectApi.remove(id)
      message.success('项目已删除')
      setProjects((prev) => prev.filter((item) => item.id !== id))
    } catch (error) {
      message.error(describeError(error))
    }
  }

  const dateText = useMemo(
    () => (iso: string) => (iso ? iso.slice(0, 16).replace('T', ' ') : ''),
    [],
  )

  return (
    <div>
      <div className="page-head">
        <div>
          <h2 className="page-title">我的项目</h2>
          <p className="page-sub">每个项目代表一部短剧，可包含多集分镜与成片。</p>
        </div>
        <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
          新建项目
        </Button>
      </div>

      {loading ? (
        <Row gutter={[16, 16]}>
          {[0, 1, 2].map((i) => (
            <Col key={i} xs={24} sm={12} lg={8} xxl={6}>
              <Card>
                <Skeleton active paragraph={{ rows: 4 }} />
              </Card>
            </Col>
          ))}
        </Row>
      ) : projects.length === 0 ? (
        <Card>
          <Empty description="还没有项目，先创建你的第一部短剧吧">
            <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
              新建项目
            </Button>
          </Empty>
        </Card>
      ) : (
        <Row gutter={[16, 16]}>
          {projects.map((p) => {
            const status = STATUS_LABEL[p.status] ?? { text: p.status, color: 'default' }
            const stage = stageOf(p)
            return (
              <Col key={p.id} xs={24} sm={12} lg={8} xxl={6}>
                <Card
                  className="hover-card jellyfish-card"
                  styles={{ body: { padding: 20 } }}
                  style={{ borderRadius: 12 }}
                >
                  {/* 顶部：分类标签 + 状态徽章 */}
                  <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 12 }}>
                    <Tag bordered={false} style={{ background: '#f1f3f5', color: '#495057', borderRadius: 4 }}>
                      {p.video_style || '未分类'}
                    </Tag>
                    <Tag color={status.color} style={{ borderRadius: 4 }}>
                      {status.text}
                    </Tag>
                  </div>

                  {/* 大标题 */}
                  <Typography.Title
                    level={4}
                    style={{ margin: 0, cursor: 'pointer', fontSize: 22 }}
                    onClick={() => navigate(`/projects/${p.id}`)}
                  >
                    {p.title}
                  </Typography.Title>
                  <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
                    {dateText(p.created_at)}
                  </div>

                  {/* 描述 */}
                  <Typography.Paragraph
                    type="secondary"
                    ellipsis={{ rows: 2 }}
                    style={{ margin: '12px 0', fontSize: 13.5 }}
                  >
                    {p.description || '暂无简介'}
                  </Typography.Paragraph>

                  {/* 当前阶段 */}
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                    <span style={{ fontSize: 13, color: '#495057' }}>当前阶段</span>
                    <Tag color={stage.color} style={{ margin: 0, borderRadius: 4 }}>
                      {stage.title}
                    </Tag>
                  </div>
                  <div className="muted" style={{ fontSize: 12.5, marginTop: 4, marginBottom: 12 }}>
                    {stage.hint}
                  </div>

                  {/* 三色统计 */}
                  <div style={{ display: 'flex', gap: 6, marginBottom: 14 }}>
                    <Tag color="warning" style={{ margin: 0, borderRadius: 4 }}>
                      待确认 {p.shots_pending}
                    </Tag>
                    <Tag color="success" style={{ margin: 0, borderRadius: 4 }}>
                      已就绪 {p.shots_ready}
                    </Tag>
                    <Tag color="processing" style={{ margin: 0, borderRadius: 4 }}>
                      生成中 {p.shots_running}
                    </Tag>
                  </div>

                  {/* 进度 */}
                  <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12.5, marginBottom: 4 }}>
                    <span>进度</span>
                    <span className="muted">{p.progress}%</span>
                  </div>
                  <Progress percent={p.progress} showInfo={false} size="small" style={{ marginBottom: 12 }} />

                  {/* 章角场道 */}
                  <div className="muted" style={{ fontSize: 12.5, marginBottom: 16 }}>
                    章 {p.episode_count} · 镜 {p.shot_count} · 角色 0 · 场 0 · 道 0
                  </div>

                  {/* 底部操作 */}
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <Button
                      type="primary"
                      icon={<ImportOutlined />}
                      block
                      onClick={() => navigate(`/projects/${p.id}`)}
                    >
                      进入分镜工作室
                    </Button>
                    <Tooltip title="编辑项目">
                      <Button
                        type="text"
                        icon={<EditOutlined style={{ color: '#4c6ef5' }} />}
                        onClick={() => openEdit(p)}
                      />
                    </Tooltip>
                    <Popconfirm
                      title="删除项目"
                      description="将同时删除其下所有分集、分镜与任务，确定吗？"
                      okText="删除"
                      cancelText="取消"
                      okButtonProps={{ danger: true }}
                      onConfirm={() => handleDelete(p.id)}
                    >
                      <Button type="text" icon={<DeleteOutlined style={{ color: '#fa5252' }} />} />
                    </Popconfirm>
                  </div>
                </Card>
              </Col>
            )
          })}
        </Row>
      )}

      <Modal
        title={editing ? '编辑项目' : '新建项目'}
        open={open}
        onCancel={() => setOpen(false)}
        onOk={handleSubmit}
        confirmLoading={saving}
        okText={editing ? '保存' : '创建'}
        cancelText="取消"
        width={640}
        destroyOnClose
        afterClose={resetForm}
      >
        <Form
          form={form}
          layout="vertical"
          initialValues={{ aspect_ratio: '9:16', style_lock: false }}
          requiredMark={false}
          style={{ marginTop: 12 }}
        >
          {!editing && (
            <div className="ai-suggest">
              <div className="ai-suggest-label">
                <ThunderboltOutlined /> 一句话灵感
              </div>
              <Input.TextArea
                rows={2}
                value={idea}
                onChange={(e) => setIdea(e.target.value)}
                placeholder="例如：一个外卖小哥发现每天深夜都会重生回到同一天"
                maxLength={300}
                showCount
              />
              <Button
                type="dashed"
                icon={<ThunderboltOutlined />}
                loading={suggesting}
                onClick={handleSuggest}
                block
                style={{ marginTop: 8 }}
              >
                AI 智能填充（标题 / 简介 / 风格）
              </Button>
            </div>
          )}

          <Form.Item
            name="title"
            label="项目名称"
            rules={[{ required: true, message: '请输入项目名称' }]}
          >
            <Input placeholder="例如：神秘复苏" maxLength={200} />
          </Form.Item>

          <Form.Item name="description" label="描述">
            <Input.TextArea rows={4} placeholder="这部剧的题材、背景、看点" maxLength={500} />
          </Form.Item>

          <Form.Item
            name="visual_style"
            label="视觉风格"
            tooltip="整张图的画法质感：写实 / 动漫 / 水墨 / 油画……"
          >
            <Select
              placeholder="选择画面质感"
              options={VISUAL_STYLES}
              allowClear
              showSearch
              optionFilterProp="label"
            />
          </Form.Item>

          <Form.Item
            name="video_style"
            label="视频风格"
            tooltip="题材赛道，会作为项目分类标签展示"
          >
            <Select
              placeholder="选择题材，如真人都市 / 古装仙侠"
              options={VIDEO_STYLES}
              allowClear
              showSearch
              optionFilterProp="label"
            />
          </Form.Item>

          <Form.Item
            name="seed"
            label="全局种子值"
            tooltip="填一个固定数字可让每次生成更可复现；留空则随机"
          >
            <InputNumber style={{ width: '100%' }} placeholder="如 75606" min={0} max={999999999} />
          </Form.Item>

          <Form.Item name="aspect_ratio" label="默认视频比例">
            <Select options={ASPECTS} />
          </Form.Item>

          <Form.Item
            name="style_lock"
            label={
              <span>
                风格统一{' '}
                <Tooltip title="开启后，所有镜头都强制沿用项目级视觉风格与种子，保证全剧画面一致">
                  <span style={{ color: '#adb5bd' }}>ⓘ</span>
                </Tooltip>
              </span>
            }
            valuePropName="checked"
          >
            <Switch />
          </Form.Item>

          <Form.Item
            name="style"
            label="整体风格关键词（高级）"
            tooltip="整部剧统一的光线、色调与质感基调，会写进所有镜头的提示词"
            style={{ marginBottom: 8 }}
          >
            <AutoComplete
              options={STYLE_OPTIONS}
              filterOption={filterStyleOption}
              placeholder="点右侧箭头从常用画风里选，也可以自己写"
            />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  )
}
