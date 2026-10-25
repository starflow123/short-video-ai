import { useCallback, useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import {
  App,
  AutoComplete,
  Alert,
  Button,
  Card,
  Checkbox,
  Col,
  Empty,
  Form,
  Image,
  Input,
  InputNumber,
  List,
  Modal,
  Popconfirm,
  Row,
  Space,
  Spin,
  Switch,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import {
  AppstoreOutlined,
  ArrowLeftOutlined,
  BulbOutlined,
  CheckCircleOutlined,
  CopyOutlined,
  DeleteOutlined,
  EditOutlined,
  EnvironmentOutlined,
  PlusOutlined,
  PictureOutlined,
  ReloadOutlined,
  RobotOutlined,
  SkinOutlined,
  TeamOutlined,
  ThunderboltOutlined,
  ToolOutlined,
  VideoCameraAddOutlined,
  WarningOutlined,
} from '@ant-design/icons'
import { useNavigate, useParams } from 'react-router-dom'

import CredentialPicker from '@/components/CredentialPicker'
import { filterStyleOption, STYLE_OPTIONS } from '@/constants/styles'
import { assetApi, episodeApi, pipelineApi, projectApi, shotApi } from '@/api/endpoints'
import { describeError, mediaUrl } from '@/api/client'
import { useCredentials } from '@/hooks/useCredentials'
import { useTaskWatch } from '@/hooks/useTaskWatch'
import type { Episode, PipelineRun, ProductionAsset, Project, Shot, ShotPlan } from '@/api/types'

const SHOT_STATUS: Record<string, { text: string; color: string }> = {
  pending: { text: '待生成', color: 'default' },
  prepared: { text: '关键帧就绪', color: 'blue' },
  generating: { text: '生成中', color: 'processing' },
  ready: { text: '已完成', color: 'success' },
  failed: { text: '失败', color: 'error' },
}

/** 流水线状态 → 展示文案与颜色（人工确认是一个独立的、需要用户介入的状态）。 */
const PIPELINE_STATUS: Record<string, { text: string; color: string }> = {
  pending: { text: '排队中', color: 'default' },
  running: { text: '正在建角色档案 / 出分镜', color: 'processing' },
  reviewing: { text: '等待人工确认提示词', color: 'warning' },
  generating: { text: '批量生图中', color: 'processing' },
  completed: { text: '已完成', color: 'success' },
  failed: { text: '失败', color: 'error' },
}

/** 项目资产分类：与后端 ASSET_KINDS 对齐，直接展示成图片卡片，方便回看与复用。 */
const ASSET_TABS: { value: string; label: string; icon: ReactNode }[] = [
  { value: 'all', label: '全部', icon: <AppstoreOutlined /> },
  { value: 'actor', label: '角色', icon: <TeamOutlined /> },
  { value: 'scene', label: '场景', icon: <EnvironmentOutlined /> },
  { value: 'prop', label: '道具', icon: <ToolOutlined /> },
  { value: 'costume', label: '服装', icon: <SkinOutlined /> },
]

const ASSET_KIND_META: Record<string, { text: string; color: string }> = {
  actor: { text: '角色', color: 'blue' },
  scene: { text: '场景', color: 'green' },
  prop: { text: '道具', color: 'orange' },
  costume: { text: '服装', color: 'purple' },
}

/** 「全部」视图下的排序：角色 → 场景 → 道具 → 服装，同类按新到旧。 */
const ASSET_KIND_ORDER = ['actor', 'scene', 'prop', 'costume']

export default function ProjectDetailPage() {
  const { projectId } = useParams<{ projectId: string }>()
  const id = Number(projectId)
  const navigate = useNavigate()
  const { message } = App.useApp()
  const { byCapability } = useCredentials()

  const [project, setProject] = useState<Project | null>(null)
  const [episodes, setEpisodes] = useState<Episode[]>([])
  const [activeId, setActiveId] = useState<number | null>(null)
  const [shots, setShots] = useState<Shot[]>([])
  const [shotsLoading, setShotsLoading] = useState(false)

  const [scriptDraft, setScriptDraft] = useState('')
  const [savingScript, setSavingScript] = useState(false)

  // 拆解分镜弹窗
  const [breakOpen, setBreakOpen] = useState(false)
  const [breakForm] = Form.useForm<{
    credential_id?: number
    shot_count?: number | null
    target_seconds?: number | null
    auto_images?: boolean
  }>()
  const [breaking, setBreaking] = useState(false)
  // 两步式拆解：先让 AI 出方案（不动镜头），用户确认或调整后再真正拆解
  const [shotPlan, setShotPlan] = useState<ShotPlan | null>(null)
  const [planning, setPlanning] = useState(false)

  // AI 一键生成弹窗（主题/世界观 → 剧本 → 分镜指令）
  const [forgeOpen, setForgeOpen] = useState(false)
  const [forgeForm] = Form.useForm<{
    credential_id?: number
    premise: string
    style?: string
    shot_count: number
    target_seconds: number
  }>()
  const [forging, setForging] = useState(false)
  const [styleBusy, setStyleBusy] = useState(false)

  // 制作流水线（LangGraph 编排）：建角色档案 → AI 出分镜 → 人工确认提示词 → 批量生图
  const [pipelineRun, setPipelineRun] = useState<PipelineRun | null>(null)
  const [pipelineOpen, setPipelineOpen] = useState(false)
  const [pipelineForm] = Form.useForm<{
    credential_id?: number
    shot_count: number
    style?: string
    rebuild_characters: boolean
  }>()
  const [pipelineBusy, setPipelineBusy] = useState(false)
  const [confirmBusy, setConfirmBusy] = useState(false)
  const [confirmShotId, setConfirmShotId] = useState<number | null>(null)

  // 项目资产（角色 / 场景 / 道具 / 服装）：以图片卡片平铺在页面顶部，方便随时回看与复用
  const [assets, setAssets] = useState<ProductionAsset[]>([])
  const [assetKind, setAssetKind] = useState('all')
  const [previewAsset, setPreviewAsset] = useState<ProductionAsset | null>(null)

  // 分镜编辑已独立成页（/projects/:projectId/shots/:shotId），卡片只做入口与删除

  const textCreds = byCapability('text')

  const activeEpisode = useMemo(
    () => episodes.find((e) => e.id === activeId) ?? null,
    [episodes, activeId],
  )

  const loadProject = useCallback(async () => {
    try {
      const [p, eps] = await Promise.all([projectApi.get(id), projectApi.listEpisodes(id)])
      setProject(p)
      setEpisodes(eps)
      setActiveId((prev) => (prev && eps.some((e) => e.id === prev) ? prev : eps[0]?.id ?? null))
    } catch (error) {
      message.error(describeError(error))
    }
  }, [id, message])

  const loadShots = useCallback(async (episodeId: number) => {
    setShotsLoading(true)
    try {
      setShots(await episodeApi.listShots(episodeId))
    } catch (error) {
      message.error(describeError(error))
    } finally {
      setShotsLoading(false)
    }
  }, [message])

  /** 取该分集最近一次流水线状态，用于恢复「等待确认 / 批量生图中」的进度提示。 */
  const loadPipeline = useCallback(
    async (episodeId: number) => {
      try {
        setPipelineRun(await pipelineApi.latest(episodeId))
      } catch {
        // 流水线状态仅用于进度展示，读取失败不影响主流程
      }
    },
    [],
  )

  useEffect(() => {
    void loadProject()
  }, [loadProject])

  /** 该项目下的资产（含勾选了「跨项目复用」的全局资产），用于顶部资产区块展示。 */
  const loadAssets = useCallback(async () => {
    try {
      setAssets(await assetApi.list({ project_id: id }))
    } catch {
      // 资产区块仅用于查看，读取失败不阻塞主流程
    }
  }, [id])

  useEffect(() => {
    void loadAssets()
  }, [loadAssets])

  const assetCounts = useMemo(() => {
    const map: Record<string, number> = { all: assets.length }
    for (const item of assets) map[item.kind] = (map[item.kind] ?? 0) + 1
    return map
  }, [assets])

  const visibleAssets = useMemo(() => {
    const list = assetKind === 'all' ? assets : assets.filter((item) => item.kind === assetKind)
    return [...list].sort(
      (a, b) =>
        ASSET_KIND_ORDER.indexOf(a.kind) - ASSET_KIND_ORDER.indexOf(b.kind) || b.id - a.id,
    )
  }, [assets, assetKind])

  const copyAssetPrompt = async (asset: ProductionAsset) => {
    const text = asset.prompt || asset.description || ''
    if (!text) {
      message.warning('该资产还没有外观描述')
      return
    }
    try {
      await navigator.clipboard.writeText(text)
      message.success('外观描述已复制，可直接粘贴到提示词里')
    } catch {
      message.warning('浏览器不允许自动复制，请手动选中复制')
    }
  }

  useEffect(() => {
    if (activeEpisode) {
      setScriptDraft(activeEpisode.script_text ?? '')
      void loadShots(activeEpisode.id)
      void loadPipeline(activeEpisode.id)
    } else {
      setShots([])
      setPipelineRun(null)
    }
  }, [activeEpisode, loadShots, loadPipeline])

  const { watch } = useTaskWatch((task) => {
    const forged = task.result?.action === 'forge'
    const broken = task.result?.action === 'breakdown'
    const isPipeline = task.kind === 'pipeline'
    if (task.status === 'failed') {
      message.error(`任务失败：${task.error ?? '未知错误'}`)
    } else if (task.kind === 'export') {
      message.success('成片已生成，可在素材库查看')
    } else if (forged) {
      message.success(`「${task.result?.title ?? '新分集'}」已生成，共 ${task.result?.shot_count ?? 0} 个分镜`)
    } else if (broken) {
      const auto = Number(task.result?.auto_image_tasks ?? 0)
      const sheets = Number(task.result?.reference_tasks ?? 0)
      const keys = Number(task.result?.keyframe_tasks ?? 0)
      const made = Number(task.result?.shot_count ?? 0)
      const planned = Number(task.result?.planned_shots ?? 0)
      const short = planned > made ? `（方案要 ${planned} 镜，模型只给出 ${made} 镜，可再拆一次补齐）` : ''
      const imageNote = auto
        ? sheets
          ? `，先生成 ${sheets} 张角色定妆照，再出 ${keys} 张关键帧（关键帧会带上人物参考图，逐个完成后刷新）`
          : `，${keys} 张关键帧排队中（逐个完成后刷新）`
        : ''
      message.success(`已按方案拆出 ${made} 个分镜${short}${imageNote}`)
    } else if (isPipeline) {
      message.success('流水线已完成本步骤，可在下方查看角色档案与分镜')
    } else {
      message.success('生成完成')
    }
    if (forged) {
      const episodeId = Number(task.result?.episode_id)
      void loadProject().then(() => {
        if (Number.isFinite(episodeId)) setActiveId(episodeId)
      })
    } else if (activeEpisode) {
      void loadShots(activeEpisode.id)
      // 拆解会把本次 plan 落到分集上，刷新分集列表才能在页面上展示拆解分析
      void loadProject()
      if (isPipeline) {
        void loadPipeline(activeEpisode.id)
        // 角色档案会在流水线里自动落库为「角色」资产，跑完顺手刷新顶部资产区块
        void loadAssets()
      }
    }
  })

  const refreshEpisodes = useCallback(async () => {
    setEpisodes(await projectApi.listEpisodes(id))
  }, [id])

  // ---------------- 分集 ----------------
  const addEpisode = async () => {
    try {
      const episode = await projectApi.createEpisode(id, {
        title: `第 ${episodes.length + 1} 集`,
      })
      await refreshEpisodes()
      setActiveId(episode.id)
    } catch (error) {
      message.error(describeError(error))
    }
  }

  const removeEpisode = async (episodeId: number) => {
    try {
      await episodeApi.remove(episodeId)
      message.success('分集已删除')
      await refreshEpisodes()
      if (activeId === episodeId) setActiveId(null)
    } catch (error) {
      message.error(describeError(error))
    }
  }

  // ---------------- 剧本 ----------------
  const saveScript = async () => {
    if (!activeEpisode) return
    setSavingScript(true)
    try {
      const updated = await episodeApi.update(activeEpisode.id, { script_text: scriptDraft })
      setEpisodes((prev) => prev.map((e) => (e.id === updated.id ? updated : e)))
      message.success('剧本已保存')
    } catch (error) {
      message.error(describeError(error))
    } finally {
      setSavingScript(false)
    }
  }

  // 第一步：只出方案，不动现有分镜
  const openBreakModal = () => {
    breakForm.setFieldsValue({
      auto_images: true,
      shot_count: activeEpisode?.shot_count || null,
      target_seconds: null,
    })
    setShotPlan(activeEpisode?.plan ?? null)
    setBreakOpen(true)
  }

  const runShotPlan = async () => {
    if (!activeEpisode) return
    const values = breakForm.getFieldsValue()
    if (!values.credential_id) {
      message.warning('请先选择文本模型密钥')
      return
    }
    setPlanning(true)
    try {
      const plan = await episodeApi.breakdownPlan(activeEpisode.id, {
        credential_id: values.credential_id,
        shot_count: values.shot_count ?? undefined,
        target_seconds: values.target_seconds ?? undefined,
      })
      setShotPlan(plan)
      breakForm.setFieldsValue({ shot_count: plan.shot_count, target_seconds: plan.target_seconds })
      message.success('拆解方案已给出，确认或调整后再生成')
    } catch (error) {
      message.error(describeError(error))
    } finally {
      setPlanning(false)
    }
  }

  // 手动微调某一段的镜头数：总数跟着走，避免方案和数量互相矛盾
  const setBeatShots = (index: number, shots: number | null) => {
    setShotPlan((prev) => {
      if (!prev) return prev
      const beats = prev.beats.map((beat, i) => (i === index ? { ...beat, shots: shots ?? 0 } : beat))
      const total = beats.reduce((sum, beat) => sum + (beat.shots || 0), 0)
      breakForm.setFieldsValue({ shot_count: total || undefined })
      return { ...prev, beats, shot_count: total }
    })
  }

  const runBreakdown = async () => {
    if (!activeEpisode) return
    const values = await breakForm.validateFields()
    if (!shotPlan && !values.shot_count) {
      message.warning('请先点「让 AI 出拆解方案」，或直接填写分镜数量')
      return
    }
    setBreaking(true)
    try {
      const task = await episodeApi.breakdown(activeEpisode.id, {
        credential_id: values.credential_id,
        shot_count: values.shot_count ?? undefined,
        target_seconds: values.target_seconds ?? undefined,
        plan: shotPlan ?? undefined,
        auto_images: values.auto_images ?? false,
      })
      watch(task.id)
      message.success('已提交分镜拆解，生成完成后自动刷新')
      setBreakOpen(false)
    } catch (error) {
      message.error(describeError(error))
    } finally {
      setBreaking(false)
    }
  }

  // ---------------- AI 一键生成 ----------------
  const openForge = () => {
    forgeForm.resetFields()
    forgeForm.setFieldsValue({ shot_count: 8, target_seconds: 60, style: project?.style ?? '' })
    setForgeOpen(true)
  }

  const runForge = async () => {
    const values = await forgeForm.validateFields()
    setForging(true)
    try {
      const task = await projectApi.forge(id, {
        credential_id: values.credential_id,
        premise: values.premise.trim(),
        style: values.style?.trim() || undefined,
        shot_count: values.shot_count,
        target_seconds: values.target_seconds,
      })
      watch(task.id)
      message.success('已开始生成剧本与分镜，完成后自动跳转')
      setForgeOpen(false)
    } catch (error) {
      message.error(describeError(error))
    } finally {
      setForging(false)
    }
  }

  /** 按当前填写的主题让 AI 定一个画面风格（复用项目建议接口，只取其中的 style）。 */
  const generateStyle = async () => {
    const premise = String(forgeForm.getFieldValue('premise') || project?.description || project?.title || '').trim()
    if (!premise) {
      message.warning('先在上面的「主题 / 世界观」里写几笔，AI 才能按内容定风格')
      return
    }
    setStyleBusy(true)
    try {
      const suggestion = await projectApi.suggest({
        idea: premise,
        aspect_ratio: project?.aspect_ratio || '9:16',
      })
      if (!suggestion.style) {
        message.warning('AI 未返回风格建议，可重试或从下拉里挑一个')
        return
      }
      forgeForm.setFieldsValue({ style: suggestion.style })
      message.success('已按主题生成风格建议，可继续微调')
    } catch (error) {
      message.error(describeError(error))
    } finally {
      setStyleBusy(false)
    }
  }

  // ---------------- 制作流水线 ----------------
  const openPipeline = () => {
    pipelineForm.resetFields()
    pipelineForm.setFieldsValue({
      shot_count: Math.max(shots.length, 8),
      style: project?.style ?? '',
      rebuild_characters: true,
    })
    setPipelineOpen(true)
  }

  const runPipeline = async () => {
    if (!activeEpisode) return
    const values = await pipelineForm.validateFields()
    setPipelineBusy(true)
    try {
      const run = await pipelineApi.start(activeEpisode.id, {
        credential_id: values.credential_id,
        shot_count: values.shot_count,
        style: values.style?.trim() || undefined,
        rebuild_characters: values.rebuild_characters,
      })
      watch(run.task_id ?? 0)
      setPipelineRun(run)
      message.success('已启动：先建角色档案，再出分镜；出完会暂停等你确认提示词')
      setPipelineOpen(false)
    } catch (error) {
      message.error(describeError(error))
    } finally {
      setPipelineBusy(false)
    }
  }

  /** 批量生图：传 shot_ids 则只跑这些镜头；不传则由后端把全部镜头标为已确认后一起跑。 */
  const runConfirm = async (shotIds: number[] | null) => {
    if (!pipelineRun) return
    setConfirmBusy(true)
    try {
      const run = await pipelineApi.confirm(pipelineRun.id, {
        shot_ids: shotIds,
      })
      setPipelineRun(run)
      if (activeEpisode) {
        await loadShots(activeEpisode.id)
        await loadPipeline(activeEpisode.id)
      }
      message.success(shotIds ? `已提交 ${shotIds.length} 个镜头生图` : '已提交批量生图，完成后自动刷新')
    } catch (error) {
      message.error(describeError(error))
    } finally {
      setConfirmBusy(false)
    }
  }

  /** 在列表上直接确认某个镜头（等价于到分镜详情页点「确认提示词」）。 */
  const confirmShot = async (shotId: number) => {
    setConfirmShotId(shotId)
    try {
      await shotApi.update(shotId, { prompt_confirmed: true })
      message.success('已确认该镜头提示词')
      if (activeEpisode) {
        await loadShots(activeEpisode.id)
        await loadPipeline(activeEpisode.id)
      }
    } catch (error) {
      message.error(describeError(error))
    } finally {
      setConfirmShotId(null)
    }
  }

  // ---------------- 分镜 ----------------
  const addShot = async () => {
    if (!activeEpisode) return
    try {
      await episodeApi.createShot(activeEpisode.id, { title: `镜头 ${shots.length + 1}` })
      await loadShots(activeEpisode.id)
      await refreshEpisodes()
    } catch (error) {
      message.error(describeError(error))
    }
  }

  const removeShot = async (shotId: number) => {
    try {
      await shotApi.remove(shotId)
      if (activeEpisode) await loadShots(activeEpisode.id)
      await refreshEpisodes()
    } catch (error) {
      message.error(describeError(error))
    }
  }

  const exportProject = async () => {
    try {
      const task = await projectApi.export(id)
      watch(task.id)
      message.success('已提交成片导出，完成后可在素材库下载')
    } catch (error) {
      message.error(describeError(error))
    }
  }

  return (
    <div>
      <div className="page-head">
        <div>
          <Button type="link" icon={<ArrowLeftOutlined />} style={{ paddingLeft: 0 }} onClick={() => navigate('/projects')}>
            返回项目列表
          </Button>
          <h2 className="page-title">{project?.title ?? '加载中…'}</h2>
          <p className="page-sub">
            {project ? `画幅 ${project.aspect_ratio}${project.style ? ` · ${project.style}` : ''}` : ''}
          </p>
        </div>
        <Space>
          <Button type="primary" icon={<RobotOutlined />} onClick={openForge}>
            AI 一键生成
          </Button>
          <Button
            icon={<TeamOutlined />}
            onClick={openPipeline}
            disabled={!activeEpisode}
          >
            角色档案流水线
          </Button>
          <Button icon={<ThunderboltOutlined />} onClick={openBreakModal} disabled={!activeEpisode}>
            AI 拆解分镜
          </Button>
          <Button icon={<VideoCameraAddOutlined />} onClick={exportProject}>
            导出成片
          </Button>
        </Space>
      </div>

      {/* 项目资产：角色 / 场景 / 道具 / 服装 以图片卡片平铺在最显眼处，方便随时回看与复用 */}
      <Card
        style={{ marginBottom: 16 }}
        styles={{ body: { padding: '14px 16px 16px' } }}
        title={
          <Space size={8} wrap>
            <span>项目资产</span>
            <Typography.Text type="secondary" style={{ fontSize: 12, fontWeight: 400 }}>
              角色 / 场景 / 道具 / 服装，生成关键帧与视频时会自动带上，保证跨镜头一致
            </Typography.Text>
          </Space>
        }
        extra={
          <Space size={4}>
            <Button size="small" type="text" icon={<ReloadOutlined />} onClick={() => void loadAssets()}>
              刷新
            </Button>
            <Button size="small" type="link" onClick={() => navigate(`/assets?project=${id}`)}>
              管理资产
            </Button>
          </Space>
        }
      >
        <div className="kind-bar" style={{ marginBottom: 14 }}>
          {ASSET_TABS.map((tab) => (
            <button
              key={tab.value}
              type="button"
              className={`kind-pill${assetKind === tab.value ? ' active' : ''}`}
              onClick={() => setAssetKind(tab.value)}
            >
              <span className="kind-icon">{tab.icon}</span>
              {tab.label}
              <span className="kind-count">{assetCounts[tab.value] ?? 0}</span>
            </button>
          ))}
        </div>

        {visibleAssets.length === 0 ? (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description={
              <span style={{ fontSize: 12.5 }}>
                还没有这类资产。点右上角「管理资产」手动新建，或跑一次角色档案流水线自动生成
              </span>
            }
          />
        ) : (
          <Row gutter={[12, 12]}>
            {visibleAssets.map((asset) => {
              const meta = ASSET_KIND_META[asset.kind] ?? { text: asset.kind, color: 'default' }
              return (
                <Col key={asset.id} xs={12} sm={8} md={6} lg={6} xxl={4}>
                  <div className="hover-card asset-tile" onClick={() => setPreviewAsset(asset)}>
                    <div className="asset-tile-cover">
                      {asset.media_url ? (
                        <img src={mediaUrl(asset.media_url)} alt={asset.name} />
                      ) : (
                        <div className="asset-tile-ph">
                          {ASSET_TABS.find((tab) => tab.value === asset.kind)?.icon ?? <PictureOutlined />}
                        </div>
                      )}
                      <Tag className="asset-tile-badge" color={meta.color} style={{ margin: 0 }}>
                        {meta.text}
                      </Tag>
                    </div>
                    <div className="asset-tile-name">{asset.name}</div>
                  </div>
                </Col>
              )
            })}
          </Row>
        )}
      </Card>

      <Row gutter={16} align="top">
        <Col xs={24} lg={6} xxl={5}>
          <Card
            title="分集"
            styles={{ body: { padding: 8 } }}
            extra={
              <Button type="text" size="small" icon={<PlusOutlined />} onClick={addEpisode}>
                新增
              </Button>
            }
          >
            <List
              dataSource={episodes}
              locale={{ emptyText: <Empty description="暂无分集" image={Empty.PRESENTED_IMAGE_SIMPLE} /> }}
              renderItem={(episode) => (
                <List.Item
                  style={{
                    cursor: 'pointer',
                    borderRadius: 10,
                    padding: '8px 10px',
                    background: episode.id === activeId ? 'var(--brand-soft)' : 'transparent',
                  }}
                  actions={[
                    <Popconfirm
                      key="del"
                      title="删除该分集？"
                      okText="删除"
                      cancelText="取消"
                      onConfirm={() => removeEpisode(episode.id)}
                    >
                      <DeleteOutlined onClick={(e) => e.stopPropagation()} />
                    </Popconfirm>,
                  ]}
                >
                  {/* 选中逻辑挂在内容容器（actions 的兄弟子树）上：Popconfirm
                      确认按钮是 React 门户，事件会沿 React 树冒泡到 List.Item，
                      挂在 List.Item 上会把 activeId 指向刚删掉的分集。 */}
                  <div
                    className="card-hit"
                    style={{ flex: 1 }}
                    onClick={() => setActiveId(episode.id)}
                  >
                    <List.Item.Meta
                      title={
                        <span style={{ fontSize: 13.5 }}>
                          {episode.order_index}. {episode.title}
                        </span>
                      }
                      description={<span style={{ fontSize: 12 }}>{episode.shot_count} 个分镜</span>}
                    />
                  </div>
                </List.Item>
              )}
            />
          </Card>
        </Col>

        <Col xs={24} lg={18} xxl={19}>
          {!activeEpisode ? (
            <Card>
              <Empty description="请选择或创建一个分集" />
            </Card>
          ) : (
            <Space direction="vertical" size={16} style={{ width: '100%' }}>
              {pipelineRun && (
                <Card
                  size="small"
                  title={
                    <Space size={8} wrap>
                      <span>制作流水线</span>
                      <Tag color={PIPELINE_STATUS[pipelineRun.status]?.color ?? 'default'} style={{ margin: 0 }}>
                        {PIPELINE_STATUS[pipelineRun.status]?.text ?? pipelineRun.status}
                      </Tag>
                      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                        已确认 {pipelineRun.confirmed_shots}/{pipelineRun.total_shots} · 已出图 {pipelineRun.generated_shots}
                      </Typography.Text>
                    </Space>
                  }
                  extra={
                    <Space>
                      <Button size="small" icon={<ReloadOutlined />} onClick={() => void loadPipeline(activeEpisode.id)}>
                        刷新状态
                      </Button>
                      <Button size="small" onClick={openPipeline}>
                        重新开始
                      </Button>
                      {pipelineRun.status === 'reviewing' && (
                        <>
                          <Button
                            size="small"
                            loading={confirmBusy}
                            disabled={pipelineRun.confirmed_shots === 0}
                            onClick={() =>
                              void runConfirm(
                                shots.filter((shot) => shot.prompt_confirmed).map((shot) => shot.id),
                              )
                            }
                          >
                            批量生图（已确认 {pipelineRun.confirmed_shots} 个）
                          </Button>
                          <Button size="small" type="primary" loading={confirmBusy} onClick={() => void runConfirm(null)}>
                            全部确认并生图
                          </Button>
                        </>
                      )}
                    </Space>
                  }
                >
                  {pipelineRun.error && (
                    <Typography.Paragraph type="danger" style={{ fontSize: 12.5, marginBottom: 8 }}>
                      <WarningOutlined /> {pipelineRun.error}
                    </Typography.Paragraph>
                  )}
                  {(pipelineRun.characters ?? []).length > 0 ? (
                    <>
                      <Typography.Text type="secondary" style={{ fontSize: 12.5 }}>
                        已建立 {pipelineRun.characters!.length} 个角色档案（长什么样 / 穿什么衣服），
                        生图时会自动带上，保证每个镜头都是同一个人：
                      </Typography.Text>
                      <div style={{ marginTop: 8, display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                        {pipelineRun.characters!.map((item) => (
                          <Tooltip
                            key={item.name}
                            title={
                              <div style={{ fontSize: 12 }}>
                                <div>{item.appearance || '未填外观'}</div>
                                <div style={{ marginTop: 4 }}>服装：{item.costume || '未填'}</div>
                              </div>
                            }
                          >
                            <Tag color="blue" style={{ margin: 0, cursor: 'help' }}>
                              {item.name}
                              {item.role ? ` · ${item.role}` : ''}
                            </Tag>
                          </Tooltip>
                        ))}
                      </div>
                    </>
                  ) : (
                    <Typography.Text type="secondary" style={{ fontSize: 12.5 }}>
                      点「重新开始」启动：AI 会先从剧本提炼角色档案（长相 + 固定服装），
                      再带着档案出分镜；出完会暂停等你逐个确认提示词，确认后再批量生图。
                    </Typography.Text>
                  )}
                </Card>
              )}

              <Card
                title="剧本"
                extra={
                  <Space>
                    <Button onClick={openBreakModal} icon={<ThunderboltOutlined />}>
                      AI 拆解
                    </Button>
                    <Button type="primary" loading={savingScript} onClick={saveScript}>
                      保存剧本
                    </Button>
                  </Space>
                }
              >
                <Input.TextArea
                  value={scriptDraft}
                  onChange={(e) => setScriptDraft(e.target.value)}
                  autoSize={{ minRows: 5, maxRows: 14 }}
                  placeholder="粘贴或撰写本集剧本：故事梗概、场景、对白……保存后可一键拆解为分镜。"
                />
              </Card>

              {activeEpisode.plan && (
                <Card
                  title="本次拆解分析"
                  extra={<Tag color="blue">{activeEpisode.plan.shot_count} 个镜头</Tag>}
                >
                  {activeEpisode.plan.reasoning && (
                    <Typography.Paragraph style={{ marginBottom: 8 }}>
                      <Typography.Text strong>为什么这样拆：</Typography.Text>
                      {activeEpisode.plan.reasoning}
                    </Typography.Paragraph>
                  )}
                  {(activeEpisode.plan.beats ?? []).length > 0 && (
                    <Space direction="vertical" size={6} style={{ width: '100%' }}>
                      {(activeEpisode.plan.beats ?? []).map((beat, index) => (
                        <div
                          key={`${beat.name}-${index}`}
                          style={{ display: 'flex', gap: 8, alignItems: 'baseline', flexWrap: 'wrap' }}
                        >
                          <Tag color="geekblue" style={{ margin: 0 }}>
                            {beat.name}
                          </Tag>
                          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                            {beat.shots} 镜 · 约 {beat.seconds} 秒
                          </Typography.Text>
                          {beat.summary && (
                            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                              {beat.summary}
                            </Typography.Text>
                          )}
                        </div>
                      ))}
                    </Space>
                  )}
                </Card>
              )}

              <div>
                <div className="page-head" style={{ marginBottom: 12 }}>
                  <div>
                    <h3 style={{ margin: 0, fontSize: 16 }}>分镜列表</h3>
                    <p className="page-sub">
                      共 {shots.length} 个镜头，点击卡片进入分镜详情，分别管理关键帧与视频。
                    </p>
                  </div>
                  <Button icon={<PlusOutlined />} onClick={addShot}>
                    新增分镜
                  </Button>
                </div>

                {shotsLoading ? (
                  <Card>
                    <Spin />
                  </Card>
                ) : shots.length === 0 ? (
                  <Card>
                    <Empty description="还没有分镜，可点击「AI 拆解分镜」自动生成" />
                  </Card>
                ) : (
                  <div className="shot-grid">
                    {shots.map((shot) => {
                      const status = SHOT_STATUS[shot.status] ?? { text: shot.status, color: 'default' }
                      // 主关键帧（用于卡片缩略图，与视频分开呈现）
                      const primary =
                        shot.keyframes.find((k) => k.is_primary && k.media_id) ??
                        shot.keyframes.find((k) => k.media_id) ??
                        null
                      const goDetail = () => navigate(`/projects/${id}/shots/${shot.id}`)
                      return (
                        <div className="shot-card" key={shot.id}>
                          {/* 点击进入独立分镜编辑页（关键帧与视频在该页分开管理） */}
                          <div className="card-hit" onClick={goDetail}>
                            <div className="shot-preview">
                              <span className="shot-index">#{shot.order_index}</span>
                              {primary?.url ? (
                                <img src={mediaUrl(primary.url)} alt={shot.title ?? ''} loading="lazy" />
                              ) : (
                                <PictureOutlined style={{ fontSize: 30, color: '#c3c9d6' }} />
                              )}
                              {shot.video_url && (
                                <span className="shot-flag">
                                  <VideoCameraAddOutlined /> 已有视频
                                </span>
                              )}
                            </div>
                            <div className="shot-body">
                              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                                <p className="shot-title">{shot.title || `镜头 ${shot.order_index}`}</p>
                                <Tag color={status.color}>{status.text}</Tag>
                              </div>
                              <p className="shot-desc">{shot.description || shot.dialogue || '暂无描述'}</p>
                              <Typography.Text type="secondary" style={{ fontSize: 11.5 }}>
                                {shot.camera ? `${shot.camera} · ` : ''}
                                {shot.duration_seconds}s · 关键帧 {shot.keyframes.length} 张
                              </Typography.Text>
                              {shot.directives && (shot.directives.mood_tags?.length || shot.directives.atmosphere) ? (
                                <div style={{ marginTop: 6, display: 'flex', flexWrap: 'wrap', gap: 4 }}>
                                  {shot.directives.atmosphere && (
                                    <Tag bordered={false} style={{ margin: 0, fontSize: 11 }}>
                                      {shot.directives.atmosphere}
                                    </Tag>
                                  )}
                                  {(shot.directives.mood_tags ?? []).map((tag) => (
                                    <Tag key={tag} color="blue" bordered={false} style={{ margin: 0, fontSize: 11 }}>
                                      {tag}
                                    </Tag>
                                  ))}
                                </div>
                              ) : null}
                            </div>
                          </div>
                          <div className="shot-actions">
                            <Button size="small" icon={<EditOutlined />} onClick={goDetail}>
                              关键帧 / 视频
                            </Button>
                            {shot.prompt_confirmed ? (
                              <Tag color="success" icon={<CheckCircleOutlined />} style={{ margin: 0 }}>
                                已确认
                              </Tag>
                            ) : (
                              <Button
                                size="small"
                                type="primary"
                                ghost
                                loading={confirmShotId === shot.id}
                                onClick={() => void confirmShot(shot.id)}
                              >
                                确认提示词
                              </Button>
                            )}
                            <Popconfirm
                              title="删除该分镜？"
                              okText="删除"
                              cancelText="取消"
                              onConfirm={() => removeShot(shot.id)}
                            >
                              <Button size="small" danger type="text" icon={<DeleteOutlined />} />
                            </Popconfirm>
                          </div>
                        </div>
                      )
                    })}
                  </div>
                )}
              </div>
            </Space>
          )}
        </Col>
      </Row>

      {/* 角色档案流水线：建角色档案 → 出分镜 → 人工确认 → 批量生图 */}
      <Modal
        title="角色档案流水线"
        open={pipelineOpen}
        onCancel={() => setPipelineOpen(false)}
        onOk={runPipeline}
        confirmLoading={pipelineBusy}
        okText="开始（先建角色档案）"
        cancelText="取消"
        width="min(640px, 92vw)"
        destroyOnClose
      >
        <Typography.Paragraph type="secondary" style={{ fontSize: 12.5, marginTop: 8 }}>
          流程：<b>① 建角色档案</b>（AI 从剧本提炼每个角色的长相与固定服装，并落库为「演员」资产）
          → <b>② AI 出分镜</b>（带着角色档案生成，保证每个镜头提示词里都是同一批人）
          → <b>③ 暂停，等你逐个确认提示词</b>（在分镜详情里可只重跑某个镜头）
          → <b>④ 确认完批量生图</b>。
        </Typography.Paragraph>
        <Form form={pipelineForm} layout="vertical" requiredMark={false}>
          <Form.Item
            name="credential_id"
            label="文本模型密钥（建档案 / 出分镜共用）"
            rules={[{ required: true, message: '请选择文本密钥' }]}
          >
            <CredentialPicker credentials={textCreds} preferProvider="deepseek" placeholder="选择 DeepSeek 等文本密钥" />
          </Form.Item>
          <Row gutter={12}>
            <Col span={12}>
              <Form.Item name="shot_count" label="分镜数量">
                <InputNumber min={1} max={60} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="style" label="画面风格（可留空用项目风格）">
                <AutoComplete options={STYLE_OPTIONS} filterOption={filterStyleOption} placeholder="电影感 / 日式动漫…" />
              </Form.Item>
            </Col>
          </Row>
          <Form.Item name="rebuild_characters" label="重新抽取角色档案" valuePropName="checked">
            <Switch />
          </Form.Item>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            关闭「重新抽取」将复用上次已建立的角色档案（更快）；开启则按当前剧本重新提炼并更新演员资产。
            流水线需要服务端安装 langgraph 依赖；实际生成仍由任务队列执行，可在「任务中心」查看进度。
          </Typography.Text>
        </Form>
      </Modal>

      {/* AI 一键生成 */}
      <Modal
        title="AI 一键生成剧本与分镜"
        open={forgeOpen}
        onCancel={() => setForgeOpen(false)}
        onOk={runForge}
        confirmLoading={forging}
        okText="开始生成"
        cancelText="取消"
        width={620}
        destroyOnClose
      >
        <Form
          form={forgeForm}
          layout="vertical"
          requiredMark={false}
          initialValues={{ shot_count: 8, target_seconds: 60 }}
          style={{ marginTop: 12 }}
        >
          <Form.Item
            name="premise"
            label="主题 / 世界观 / 设定"
            rules={[{ required: true, message: '请描述主题或世界观' }]}
          >
            <Input.TextArea
              autoSize={{ minRows: 4, maxRows: 9 }}
              placeholder="例如：深夜便利店，一位加班到凌晨的女孩与夜班店员的一段温柔邂逅。"
            />
          </Form.Item>
          <Row gutter={12}>
            <Col span={12}>
              <Form.Item
                name="credential_id"
                label="文本模型密钥"
                rules={[{ required: true, message: '请选择文本密钥' }]}
              >
                <CredentialPicker credentials={textCreds} preferProvider="deepseek" placeholder="选择 DeepSeek 等文本密钥" />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item
                name="style"
                label={
                  <Space size={8}>
                    <span>画面风格</span>
                    <Button
                      size="small"
                      type="link"
                      icon={<BulbOutlined />}
                      loading={styleBusy}
                      onClick={() => void generateStyle()}
                      style={{ padding: 0, height: 'auto', fontSize: 12 }}
                    >
                      AI 生成
                    </Button>
                  </Space>
                }
                style={{ marginBottom: 8 }}
              >
                <AutoComplete
                  options={STYLE_OPTIONS}
                  filterOption={filterStyleOption}
                  placeholder="从常用画风里选一个，或自己写"
                />
              </Form.Item>
              <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginBottom: 0 }}>
                「画面风格」= 整部剧统一的光线、色调与质感基调，会写进所有镜头的提示词。
                不了解画风就点「AI 生成」按上面的主题自动定一个，或从下拉里挑（如「电影感」「日式动漫」）。
              </Typography.Paragraph>
            </Col>
          </Row>
          <Row gutter={12}>
            <Col span={12}>
              <Form.Item name="shot_count" label="分镜数量">
                <InputNumber min={1} max={60} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="target_seconds" label="本集时长目标（秒）">
                <InputNumber min={15} max={600} step={15} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
          </Row>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            将由 AI 自动完成「编剧 → 分镜指令」两步：生成一集剧本、自动创建分集，
            并按 Jellyfish 式镜头语言输出景别 / 机位 / 运镜 / 氛围 / 动作拍点与关键帧、视频提示词。
          </Typography.Text>
        </Form>
      </Modal>

      {/* 拆解分镜：先出方案 → 用户确认或改 → 再拆解（拆解会整批替换旧分镜） */}
      <Modal
        title="AI 拆解分镜"
        open={breakOpen}
        onCancel={() => setBreakOpen(false)}
        onOk={runBreakdown}
        confirmLoading={breaking}
        okText={shotPlan ? '按这份方案拆解' : '开始拆解'}
        cancelText="取消"
        width="min(760px, 94vw)"
        destroyOnClose
      >
        <Form
          form={breakForm}
          layout="vertical"
          requiredMark={false}
          style={{ marginTop: 12 }}
        >
          <Form.Item name="credential_id" label="文本模型密钥" rules={[{ required: true, message: '请选择文本密钥' }]}>
            <CredentialPicker credentials={textCreds} preferProvider="deepseek" placeholder="选择 DeepSeek 等文本密钥" />
          </Form.Item>
          <Row gutter={12}>
            <Col span={12}>
              <Form.Item name="shot_count" label="分镜数量" extra="留空由 AI 方案决定">
                <InputNumber min={1} max={60} style={{ width: '100%' }} placeholder="留空由 AI 决定" />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="target_seconds" label="本集时长目标（秒，可选）">
                <InputNumber min={15} max={600} step={15} style={{ width: '100%' }} placeholder="可选" />
              </Form.Item>
            </Col>
          </Row>
          <Form.Item name="auto_images" valuePropName="checked" noStyle>
            <Checkbox>拆解完成后自动生成定妆照与每个镜头的关键帧</Checkbox>
          </Form.Item>
        </Form>

        {activeEpisode && activeEpisode.shot_count > 0 && (
          <Alert
            style={{ marginTop: 12 }}
            type="warning"
            showIcon
            message={`这是重新拆解：旧的 ${activeEpisode.shot_count} 个分镜会被整批删除`}
            description="旧分镜独占的关键帧图片与视频会一并彻底删除，无法恢复；已挂到角色上的定妆参考图不受影响。"
          />
        )}

        <Space style={{ marginTop: 12 }}>
          <Button icon={<RobotOutlined />} onClick={runShotPlan} loading={planning}>
            {shotPlan ? '重新出方案' : '第 1 步：让 AI 出拆解方案'}
          </Button>
        </Space>

        {shotPlan && (
          <div style={{ marginTop: 12, display: 'grid', gap: 8 }}>
            <Alert
              type="info"
              message={
                `建议 ${shotPlan.shot_count} 个镜头` +
                (shotPlan.shot_seconds ? ` · 每镜约 ${shotPlan.shot_seconds} 秒` : '') +
                (shotPlan.target_seconds ? ` · 成片约 ${shotPlan.target_seconds} 秒` : '') +
                (shotPlan.confirmed ? ' · 本集上一次拆解沿用的方案' : '')
              }
              description={shotPlan.reasoning || undefined}
            />
            {(shotPlan.beats ?? []).map((beat, index) => (
              <div className="plan-beat" key={`${beat.name}-${index}`}>
                <Space align="start" style={{ width: '100%', justifyContent: 'space-between' }}>
                  <div>
                    <Typography.Text strong>{index + 1}. {beat.name}</Typography.Text>
                    {beat.summary && (
                      <div>
                        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                          {beat.summary}
                        </Typography.Text>
                      </div>
                    )}
                  </div>
                  <Space size={4}>
                    <InputNumber
                      size="small"
                      min={0}
                      max={60}
                      value={beat.shots}
                      onChange={(value) => setBeatShots(index, typeof value === 'number' ? value : null)}
                    />
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>镜</Typography.Text>
                  </Space>
                </Space>
              </div>
            ))}
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              逐镜的时长与分辨率（720p / 1080p）由 AI 按每个镜头的重要程度分别决定，不再整集统一。
            </Typography.Text>
          </div>
        )}
      </Modal>

      {/* 资产预览：点卡片看参考大图与外观描述，并可一键复制到提示词 */}
      <Modal
        title={previewAsset?.name}
        open={!!previewAsset}
        onCancel={() => setPreviewAsset(null)}
        footer={null}
        width="min(680px, 92vw)"
        destroyOnClose
      >
        {previewAsset && (
          <Space direction="vertical" size={12} style={{ width: '100%' }}>
            <Space size={6} wrap>
              <Tag color={(ASSET_KIND_META[previewAsset.kind] ?? { color: 'default' }).color} style={{ margin: 0 }}>
                {(ASSET_KIND_META[previewAsset.kind] ?? { text: previewAsset.kind }).text}
              </Tag>
              {previewAsset.is_global && (
                <Tag color="gold" style={{ margin: 0 }}>
                  跨项目
                </Tag>
              )}
              {previewAsset.source === 'generated' && (
                <Tag color="cyan" style={{ margin: 0 }}>
                  自动提取
                </Tag>
              )}
            </Space>
            {previewAsset.media_url && (
              <Image src={mediaUrl(previewAsset.media_url)} style={{ width: '100%', borderRadius: 10 }} />
            )}
            <div>
              <Typography.Text type="secondary" style={{ fontSize: 12.5 }}>
                外观描述（生成时会原样拼进提示词）
              </Typography.Text>
              <Typography.Paragraph style={{ margin: '6px 0 0', fontSize: 13.5 }}>
                {previewAsset.prompt || previewAsset.description || '暂无外观描述'}
              </Typography.Paragraph>
            </div>
            <Button icon={<CopyOutlined />} onClick={() => void copyAssetPrompt(previewAsset)}>
              复制外观描述
            </Button>
          </Space>
        )}
      </Modal>

    </div>
  )
}
