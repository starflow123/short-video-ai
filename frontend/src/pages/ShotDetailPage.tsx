import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Alert,
  App,
  Button,
  Card,
  Checkbox,
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
  Space,
  Spin,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import {
  ArrowLeftOutlined,
  BulbOutlined,
  CheckCircleOutlined,
  DeleteOutlined,
  FullscreenOutlined,
  PictureOutlined,
  PlusOutlined,
  ReloadOutlined,
  SaveOutlined,
  StarFilled,
  StarOutlined,
  TeamOutlined,
  VideoCameraAddOutlined,
} from '@ant-design/icons'
import { useNavigate, useParams } from 'react-router-dom'

import CredentialPicker from '@/components/CredentialPicker'
import PromptTemplatePicker from '@/components/PromptTemplatePicker'
import { applyStyleKeyword, STYLE_PRESETS } from '@/constants/styles'
import { describeError, mediaUrl } from '@/api/client'
import { assetApi, projectApi, shotApi } from '@/api/endpoints'
import { useCredentials } from '@/hooks/useCredentials'
import { useTaskWatch } from '@/hooks/useTaskWatch'
import type {
  ProductionAsset,
  Project,
  PromptPreview,
  Shot,
  ShotActionBeat,
  ShotFieldKey,
  ShotKeyframe,
  ShotPayload,
  ShotProp,
  ShotState,
  ShotSubject,
} from '@/api/types'

const SHOT_STATUS: Record<string, { text: string; color: string }> = {
  pending: { text: '待生成', color: 'default' },
  prepared: { text: '关键帧就绪', color: 'blue' },
  generating: { text: '生成中', color: 'processing' },
  ready: { text: '已完成', color: 'success' },
  failed: { text: '失败', color: 'error' },
}

/** 一键生成三张关键帧时的默认标签与构图提示（起幅 / 中段 / 落幅），每张可各自填写提示词。 */
const KEYFRAME_PRESETS = [
  { key: 'start', label: '起幅', hint: '构图为镜头起幅：交代环境、人物站位与景别' },
  { key: 'middle', label: '中段', hint: '构图为镜头中段：动作推进到最有张力的瞬间' },
  { key: 'end', label: '落幅', hint: '构图为镜头落幅：情绪收束，画面定格' },
]

/** 把构图提示拼到基础提示词后；基础提示词为空时只用构图提示。 */
function composePrompt(base: string, hint: string) {
  return base ? `${base}。${hint}` : hint
}

const RESOLUTIONS = ['720p', '1080p']

/** 镜头语言分层字段：顺序即注入顺序，前四项是模型区别对待的硬信号。 */
const DIRECTIVE_FIELDS: {
  key: ShotFieldKey
  label: string
  placeholder: string
  md?: number
  options?: string[]
}[] = [
  {
    key: 'shot_size',
    label: '景别',
    placeholder: '中近景 / 全景 / 特写',
    options: ['大远景', '全景', '中景', '中近景', '近景', '特写', '大特写'],
  },
  {
    key: 'camera_angle',
    label: '机位 / 角度',
    placeholder: '平视 / 略俯拍 / 仰拍',
    options: ['平视', '略俯拍', '俯拍', '低角度仰拍', '过肩', '主观视角'],
  },
  {
    key: 'camera_movement',
    label: '运镜',
    placeholder: '固定机位 / 缓慢推近',
    options: ['固定机位', '缓慢推近', '缓慢拉远', '横向摇移', '跟随', '环绕'],
  },
  {
    key: 'composition',
    label: '构图与空间锚点',
    placeholder: '林越居左前景，陈默居右中景',
    md: 12,
  },
  {
    key: 'atmosphere',
    label: '氛围（光位与冷暖）',
    placeholder: '夜间教室，顶光冷白，窗外青黑雾气',
    md: 12,
  },
  {
    key: 'mood_tags',
    label: '情绪标签',
    placeholder: '警觉、压抑、不安（用「、」分隔）',
    md: 24,
  },
]

/** 表单字段标题，右侧带一个「AI 生成」按钮（不了解怎么写的字段交给 AI）。 */
function FieldLabel({
  text,
  busy,
  onClick,
}: {
  text: string
  busy?: boolean
  onClick?: () => void
}) {
  return (
    <Space size={8}>
      <span>{text}</span>
      {onClick && (
        <Button
          size="small"
          type="link"
          icon={<BulbOutlined />}
          loading={busy}
          onClick={onClick}
          style={{ padding: 0, height: 'auto', fontSize: 12 }}
        >
          AI 生成
        </Button>
      )}
    </Space>
  )
}

export default function ShotDetailPage() {
  const { projectId, shotId } = useParams<{ projectId: string; shotId: string }>()
  const navigate = useNavigate()
  const { message } = App.useApp()
  const { byCapability } = useCredentials()

  const [shot, setShot] = useState<Shot | null>(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [form] = Form.useForm<ShotPayload>()
  // 这两段是编译输入的「正文」，最终提示词还会叠加镜头语言、时间轴等层
  const imagePromptValue = Form.useWatch('image_prompt', form)
  const videoPromptValue = Form.useWatch('video_prompt', form)

  const [preview, setPreview] = useState<{ kind: 'image' | 'video'; url: string; title: string } | null>(null)

  // 生成关键帧
  const [kfOpen, setKfOpen] = useState(false)
  const [kfForm] = Form.useForm<{
    credential_id?: number
    style?: string
    target?: number | 'new'
    label?: string
    prompt?: string
    batch: boolean
    batchPrompts?: Record<string, string>
    asset_ids?: number[]
    negative_prompt?: string
  }>()
  const [kfBusy, setKfBusy] = useState(false)

  // 生成视频
  const [videoOpen, setVideoOpen] = useState(false)
  const [videoForm] = Form.useForm<{
    credential_id?: number
    prompt?: string
    resolution?: string
    duration?: number
    image_media_id?: number
    asset_ids?: number[]
    negative_prompt?: string
  }>()
  const [videoBusy, setVideoBusy] = useState(false)

  // 关联资产：项目内的角色 / 场景 / 道具 / 服装，生成时把外观描述拼进提示词保证一致性
  const [assets, setAssets] = useState<ProductionAsset[]>([])
  const [project, setProject] = useState<Project | null>(null)
  const [confirming, setConfirming] = useState(false)

  // 结构化镜头上下文：AI 拆解产出的事实（谁拿什么、几秒做什么），可在这里就地纠正。
  // 生成时按它选参考图、拼主体绑定与时间轴，所以改完不必重跑整集拆解。
  const [ctxSubjects, setCtxSubjects] = useState<ShotSubject[]>([])
  const [ctxProps, setCtxProps] = useState<ShotProp[]>([])
  const [ctxTimeline, setCtxTimeline] = useState<ShotActionBeat[]>([])
  const [ctxStart, setCtxStart] = useState<ShotState>({})
  const [ctxEnd, setCtxEnd] = useState<ShotState>({})
  const [ctxSaving, setCtxSaving] = useState(false)

  // 镜头语言分层：景别 / 机位 / 运镜 / 构图 / 氛围 / 情绪各自独立填写，
  // 也可逐层 AI 生成，改一层不影响其它层；合成提示词由后端统一编译。
  const [ctxDirectives, setCtxDirectives] = useState<Record<string, string>>({})
  const [directiveBusy, setDirectiveBusy] = useState<string | null>(null)
  const [directiveSaving, setDirectiveSaving] = useState(false)

  // 拼装预览：生成前先看真实提示词与各层字数，避免改了却不知道哪块被裁掉
  const [previewOpen, setPreviewOpen] = useState(false)
  const [previewKind, setPreviewKind] = useState<'image' | 'video'>('image')
  const [previewLoading, setPreviewLoading] = useState(false)
  const [previewData, setPreviewData] = useState<PromptPreview | null>(null)

  // AI 生成单个字段（画面描述 / 关键帧提示词 / 视频提示词）；aiBusy 记录当前正在生成的按钮
  const [aiBusy, setAiBusy] = useState<string | null>(null)
  const [batchAiBusy, setBatchAiBusy] = useState(false)

  const imageCreds = byCapability('image')
  const videoCreds = byCapability('video')

  const keyframes = shot?.keyframes ?? []
  const primaryKeyframe = useMemo(() => {
    const flagged = keyframes.find((item) => item.is_primary)
    if (flagged?.media_id) return flagged
    return keyframes.find((item) => item.media_id) ?? flagged ?? null
  }, [keyframes])

  const status = shot ? SHOT_STATUS[shot.status] ?? { text: shot.status, color: 'default' } : null

  const load = useCallback(async () => {
    try {
      const data = await shotApi.get(Number(shotId))
      setShot(data)
      form.setFieldsValue({
        title: data.title ?? '',
        description: data.description ?? '',
        dialogue: data.dialogue ?? '',
        camera: data.camera ?? '',
        duration_seconds: data.duration_seconds,
        resolution: data.resolution ?? undefined,
        image_prompt: data.image_prompt ?? '',
        video_prompt: data.video_prompt ?? '',
      })
      setCtxSubjects(data.subjects ?? [])
      setCtxProps(data.props ?? [])
      setCtxTimeline(data.action_timeline ?? [])
      setCtxStart(data.start_state ?? {})
      setCtxEnd(data.end_state ?? {})
      const directives = (data.directives ?? {}) as Record<string, unknown>
      setCtxDirectives(
        Object.fromEntries(
          ['shot_size', 'camera_angle', 'camera_movement', 'composition', 'atmosphere', 'mood_tags'].map(
            (key) => {
              const value = directives[key]
              return [key, Array.isArray(value) ? value.join('、') : String(value ?? '')]
            },
          ),
        ),
      )
    } catch (error) {
      message.error(describeError(error))
    } finally {
      setLoading(false)
    }
  }, [shotId, form, message])

  useEffect(() => {
    void load()
  }, [load])

  const { watch } = useTaskWatch((task) => {
    if (task.status === 'failed') {
      message.error(`任务失败：${task.error ?? '未知错误'}`)
    } else {
      message.success(task.kind === 'video' ? '视频已生成' : '关键帧已生成')
    }
    void load()
  })

  // 拉取项目信息与项目内的资产：分别用于模板变量渲染与「关联资产」下拉
  const loadProjectAssets = useCallback(async () => {
    if (!projectId) return
    try {
      const [projectInfo, assetList] = await Promise.all([
        projectApi.get(Number(projectId)),
        assetApi.list({ project_id: Number(projectId) }),
      ])
      setProject(projectInfo)
      setAssets(assetList)
    } catch {
      // 资产仅用于增强提示词，拉取失败不阻塞生成
    }
  }, [projectId])

  useEffect(() => {
    void loadProjectAssets()
  }, [loadProjectAssets])

  /** 模板变量：把当前镜头上下文喂给模板，渲染后即可直接用于生成。 */
  const templateContext = useMemo<Record<string, string>>(
    () => ({
      description: shot?.description ?? shot?.image_prompt ?? '',
      visual_style: project?.visual_style ?? '',
      style: project?.style ?? '',
      aspect_ratio: project?.aspect_ratio ?? '9:16',
    }),
    [shot, project],
  )

  /** 资产下拉项：按类型分组，方便一次勾选「角色 + 场景」保持整段戏一致。 */
  const assetOptions = useMemo(() => {
    const labels: Record<string, string> = { actor: '角色', scene: '场景', prop: '道具', costume: '服装' }
    const buckets = new Map<string, { label: string; options: { value: number; label: string }[] }>()
    for (const asset of assets) {
      const key = asset.kind || 'other'
      if (!buckets.has(key)) buckets.set(key, { label: labels[key] ?? key, options: [] })
      buckets.get(key)!.options.push({ value: asset.id, label: asset.name })
    }
    return [...buckets.values()]
  }, [assets])

  /** 主体绑定资产用：只列角色（含服装），绑定后生成时才会取对应参考图。 */
  const actorOptions = useMemo(
    () =>
      assets
        .filter((item) => item.kind === 'actor' || item.kind === 'costume')
        .map((item) => ({ value: item.id, label: `#${item.id} ${item.name}` })),
    [assets],
  )

  /** 人工确认：确认该镜头的提示词，之后才能进入批量生图。 */
  const confirmPrompt = async () => {
    if (!shot) return
    setConfirming(true)
    try {
      await shotApi.update(shot.id, { prompt_confirmed: true })
      message.success('已确认该镜头提示词，可用于批量生图')
      await load()
    } catch (error) {
      message.error(describeError(error))
    } finally {
      setConfirming(false)
    }
  }

  const saveShot = async () => {
    if (!shot) return
    const values = await form.validateFields()
    setSaving(true)
    try {
      await shotApi.update(shot.id, values)
      message.success('镜头属性已保存')
      await load()
    } catch (error) {
      message.error(describeError(error))
    } finally {
      setSaving(false)
    }
  }

  // ---------------- 结构化镜头上下文 ----------------
  const patchSubject = (index: number, patch: Partial<ShotSubject>) =>
    setCtxSubjects((prev) => prev.map((item, i) => (i === index ? { ...item, ...patch } : item)))
  const patchProp = (index: number, patch: Partial<ShotProp>) =>
    setCtxProps((prev) => prev.map((item, i) => (i === index ? { ...item, ...patch } : item)))
  const patchBeat = (index: number, patch: Partial<ShotActionBeat>) =>
    setCtxTimeline((prev) => prev.map((item, i) => (i === index ? { ...item, ...patch } : item)))
  const patchState = (which: 'start' | 'end', key: keyof ShotState, value: string) => {
    const apply = (prev: ShotState) => ({ ...prev, [key]: value })
    if (which === 'start') setCtxStart(apply)
    else setCtxEnd(apply)
  }

  /** 追加拍点：从上一拍收尾处接着写，避免出现空档。 */
  const addBeat = () => {
    const total = Number(shot?.duration_seconds) || 5
    setCtxTimeline((prev) => {
      const last = prev[prev.length - 1]
      const start = last ? Number(last.end) || 0 : 0
      const end = Math.min(total, start + 3)
      return [...prev, { start, end: end > start ? end : start + 1, action: '' }]
    })
  }

  /** 逐层生成单个镜头语言字段：只回填这一层，不动其它层与整段提示词。 */
  const suggestDirective = async (key: ShotFieldKey) => {
    if (!shot) return
    setDirectiveBusy(key)
    try {
      const result = await shotApi.suggest(shot.id, { field: key })
      setCtxDirectives((prev) => ({ ...prev, [key]: result.value }))
      message.success('已生成，可修改后再保存')
    } catch (error) {
      message.error(describeError(error))
    } finally {
      setDirectiveBusy(null)
    }
  }

  const saveDirectives = async () => {
    if (!shot) return
    setDirectiveSaving(true)
    try {
      const tags = (ctxDirectives.mood_tags ?? '')
        .split(/[、,，]/)
        .map((item) => item.trim())
        .filter(Boolean)
      const payload = {
        shot_size: ctxDirectives.shot_size?.trim() || null,
        camera_angle: ctxDirectives.camera_angle?.trim() || null,
        camera_movement: ctxDirectives.camera_movement?.trim() || null,
        composition: ctxDirectives.composition?.trim() || null,
        atmosphere: ctxDirectives.atmosphere?.trim() || null,
        mood_tags: tags.length ? tags : null,
      }
      await shotApi.update(shot.id, { directives: payload })
      message.success('镜头语言已保存，会注入到本镜的图片与视频提示词')
      await load()
    } catch (error) {
      message.error(describeError(error))
    } finally {
      setDirectiveSaving(false)
    }
  }

  const openPreview = async (kind: 'image' | 'video') => {
    if (!shot) return
    setPreviewKind(kind)
    setPreviewOpen(true)
    setPreviewLoading(true)
    setPreviewData(null)
    try {
      const data = await shotApi.previewPrompt(shot.id, { kind })
      setPreviewData(data)
    } catch (error) {
      message.error(describeError(error))
      setPreviewOpen(false)
    } finally {
      setPreviewLoading(false)
    }
  }

  /** 保存结构化上下文。只改这五个字段，不触发重新拆解；
   * 保存后重生成时才会按新的主体 / 道具归属 / 拍点解析参考图与提示词。
   */
  const saveContext = async () => {
    if (!shot) return
    if (ctxSubjects.some((item) => !item.name.trim())) {
      message.warning('主体姓名不能为空，请补全或删除该行')
      return
    }
    const badBeat = ctxTimeline.find((beat) => !(Number(beat.end) > Number(beat.start)))
    if (badBeat) {
      message.warning('时间轴拍点必须满足「结束秒数 > 开始秒数」')
      return
    }
    const badOwner = ctxProps.find(
      (item) => item.name.trim() && item.owner && !ctxSubjects.some((subject) => subject.name === item.owner),
    )
    if (badOwner) {
      message.warning(`道具「${badOwner.name}」的持有者「${badOwner.owner}」不在出场主体里`)
      return
    }
    setCtxSaving(true)
    try {
      const hasState = (state: ShotState) => Object.values(state).some((value) => String(value ?? '').trim())
      await shotApi.update(shot.id, {
        subjects: ctxSubjects.filter((item) => item.name.trim()),
        props: ctxProps.filter((item) => item.name.trim()),
        action_timeline: ctxTimeline,
        start_state: hasState(ctxStart) ? ctxStart : null,
        end_state: hasState(ctxEnd) ? ctxEnd : null,
      })
      message.success('已保存；点「生成关键帧」或「生成视频」只会重跑本镜')
      await load()
    } catch (error) {
      message.error(describeError(error))
    } finally {
      setCtxSaving(false)
    }
  }

  // ---------------- AI 生成字段 ----------------
  /** 调后端为分镜的某个字段生成内容（自动带入项目风格与前后镜头上下文）。 */
  const runSuggest = async (field: 'description' | 'image_prompt' | 'video_prompt', hint?: string) => {
    if (!shot) throw new Error('分镜不存在')
    return (await shotApi.suggest(shot.id, { field, hint })).value
  }

  /** 生成镜头属性里的字段，填入表单（需再点「保存」写入）。 */
  const generateField = async (field: 'description' | 'image_prompt' | 'video_prompt') => {
    setAiBusy(field)
    try {
      const value = await runSuggest(field)
      if (field === 'description') form.setFieldsValue({ description: value })
      else if (field === 'image_prompt') form.setFieldsValue({ image_prompt: value })
      else form.setFieldsValue({ video_prompt: value })
      message.success('已生成，确认后点「保存」写入分镜')
    } catch (error) {
      message.error(describeError(error))
    } finally {
      setAiBusy(null)
    }
  }

  /** 为「起幅 / 中段 / 落幅」三张各生成一版提示词，直接填进批量输入框。 */
  const generateBatchPrompts = async () => {
    setBatchAiBusy(true)
    try {
      const entries = await Promise.all(
        KEYFRAME_PRESETS.map(async (preset) => {
          const value = await runSuggest('image_prompt', preset.hint)
          return [preset.key, value] as const
        }),
      )
      kfForm.setFieldsValue({ batchPrompts: Object.fromEntries(entries) })
      message.success('已为起幅 / 中段 / 落幅分别生成提示词')
    } catch (error) {
      message.error(describeError(error))
    } finally {
      setBatchAiBusy(false)
    }
  }

  /** 生成关键帧弹窗 / 视频弹窗里的提示词（由调用方决定写入哪个表单）。 */
  const generateInto = async (
    key: string,
    field: 'image_prompt' | 'video_prompt',
    apply: (value: string) => void,
    hint?: string,
  ) => {
    setAiBusy(key)
    try {
      apply(await runSuggest(field, hint))
    } catch (error) {
      message.error(describeError(error))
    } finally {
      setAiBusy(null)
    }
  }

  // ---------------- 关键帧 ----------------
  const openKeyframeModal = (target?: ShotKeyframe) => {
    kfForm.resetFields()
    const base = target?.prompt ?? shot?.image_prompt ?? shot?.description ?? ''
    kfForm.setFieldsValue({
      target: target?.id ?? 'new',
      label: target?.label ?? '',
      prompt: base,
      style: undefined,
      batch: false,
      // 每张关键帧预填各自的构图提示，让三张默认就不一样，且都能单独改
      batchPrompts: target
        ? undefined
        : Object.fromEntries(KEYFRAME_PRESETS.map((preset) => [preset.key, composePrompt(base, preset.hint)])),
    })
    setKfOpen(true)
  }

  const addKeyframeSlot = async () => {
    if (!shot) return
    try {
      await shotApi.createKeyframe(shot.id, {
        label: `关键帧 ${keyframes.length + 1}`,
        prompt: shot.image_prompt ?? shot.description ?? null,
      })
      message.success('已新增一个关键帧位，可点击「生成」出图')
      await load()
    } catch (error) {
      message.error(describeError(error))
    }
  }

  const submitKeyframe = async () => {
    if (!shot) return
    const values = await kfForm.validateFields()
    const base = (values.prompt ?? '').trim() || shot.image_prompt || shot.description || ''
    setKfBusy(true)
    try {
      if (values.batch) {
        // 【逐张提示词】把「起幅 / 中段 / 落幅」三个输入框各自的值按顺序组成 keyframe_prompts 下发，
        // 后端第 i 张就用第 i 条提示词（而不是三张共用同一句再叠加构图提示）。
        // 留空的条目传空串，后端按「分镜提示词 + 阶段说明」兜底。
        const batchPrompts = values.batchPrompts ?? {}
        const keyframePrompts = KEYFRAME_PRESETS.map((preset) =>
          applyStyleKeyword((batchPrompts[preset.key] ?? '').trim() || base, values.style),
        )
        const task = await shotApi.generateImage(shot.id, {
          credential_id: values.credential_id,
          n: 3,
          keyframe_id: null,
          keyframe_prompts: keyframePrompts,
          asset_ids: values.asset_ids?.length ? values.asset_ids : undefined,
          negative_prompt: values.negative_prompt?.trim() ? values.negative_prompt : undefined,
        })
        watch(task.id)
        message.success('已提交 3 张关键帧（起幅 / 中段 / 落幅），每张各用自己的提示词，后端顺序生成中')
      } else {
        const targetId = values.target === 'new' ? null : Number(values.target)
        const task = await shotApi.generateImage(shot.id, {
          credential_id: values.credential_id,
          prompt: applyStyleKeyword(base, values.style) || undefined,
          keyframe_id: targetId,
          keyframe_label: values.label || undefined,
          asset_ids: values.asset_ids?.length ? values.asset_ids : undefined,
          negative_prompt: values.negative_prompt?.trim() ? values.negative_prompt : undefined,
        })
        watch(task.id)
        message.success('已提交关键帧生成，完成后自动刷新')
      }
      setKfOpen(false)
    } catch (error) {
      message.error(describeError(error))
    } finally {
      setKfBusy(false)
    }
  }

  const setPrimary = async (keyframe: ShotKeyframe) => {
    if (!shot) return
    try {
      await shotApi.updateKeyframe(shot.id, keyframe.id, { is_primary: true })
      await load()
    } catch (error) {
      message.error(describeError(error))
    }
  }

  const removeKeyframe = async (keyframe: ShotKeyframe) => {
    if (!shot) return
    try {
      await shotApi.removeKeyframe(shot.id, keyframe.id)
      message.success('已移除该关键帧（图片仍保留在素材库）')
      await load()
    } catch (error) {
      message.error(describeError(error))
    }
  }

  // ---------------- 视频 ----------------
  const openVideoModal = () => {
    if (!shot) return
    videoForm.resetFields()
    videoForm.setFieldsValue({
      prompt: shot.video_prompt ?? shot.description ?? '',
      duration: shot.duration_seconds,
      // AI 拆解时按本镜重要程度选好的分辨率，留在这里当默认值
      resolution: shot.resolution ?? undefined,
      image_media_id: primaryKeyframe?.media_id ?? undefined,
    })
    setVideoOpen(true)
  }

  const submitVideo = async () => {
    if (!shot) return
    const values = await videoForm.validateFields()
    setVideoBusy(true)
    try {
      const task = await shotApi.generateVideo(shot.id, {
        credential_id: values.credential_id,
        prompt: values.prompt?.trim() || undefined,
        resolution: values.resolution,
        duration: values.duration ?? undefined,
        image_media_id: values.image_media_id ?? undefined,
        asset_ids: values.asset_ids?.length ? values.asset_ids : undefined,
        negative_prompt: values.negative_prompt?.trim() ? values.negative_prompt : undefined,
      })
      watch(task.id)
      message.success('已提交视频生成，完成后自动刷新')
      setVideoOpen(false)
    } catch (error) {
      message.error(describeError(error))
    } finally {
      setVideoBusy(false)
    }
  }

  if (loading) {
    return (
      <Card>
        <Spin />
      </Card>
    )
  }

  if (!shot) {
    return (
      <Card>
        <Empty description="分镜不存在" />
      </Card>
    )
  }

  // 前后镜头：生成关键帧 / 视频时后端会自动把这部分上下文拼进提示词
  const neighbors = [
    { key: 'prev', label: '上一镜头', order: shot.prev_shot?.order_index, title: shot.prev_shot?.title, summary: shot.prev_shot?.summary },
    { key: 'next', label: '下一镜头', order: shot.next_shot?.order_index, title: shot.next_shot?.title, summary: shot.next_shot?.summary },
  ].filter((item) => item.order != null)

  return (
    <div>
      <div className="page-head">
        <div>
          <Button
            type="link"
            icon={<ArrowLeftOutlined />}
            style={{ paddingLeft: 0 }}
            onClick={() => navigate(`/projects/${projectId}`)}
          >
            返回分集分镜
          </Button>
          <h2 className="page-title">
            镜头 {shot.order_index}
            {shot.title ? ` · ${shot.title}` : ''}
          </h2>
          <p className="page-sub">
            关键帧与视频分开管理：先备好关键帧（可多张，其中一张为主帧），再用主帧生成视频。
          </p>
        </div>
        <Space>
          {status && <Tag color={status.color}>{status.text}</Tag>}
          {shot.prompt_confirmed ? (
            <Tag color="success" icon={<CheckCircleOutlined />}>
              提示词已确认
            </Tag>
          ) : (
            <Tooltip title="确认后该镜头才能进入「批量生图」；改过提示词建议重新确认">
              <Button
                icon={<CheckCircleOutlined />}
                loading={confirming}
                onClick={() => void confirmPrompt()}
              >
                确认提示词
              </Button>
            </Tooltip>
          )}
          <Button icon={<PictureOutlined />} onClick={() => openKeyframeModal()}>
            生成关键帧
          </Button>
          <Button type="primary" icon={<VideoCameraAddOutlined />} onClick={openVideoModal}>
            生成视频
          </Button>
        </Space>
      </div>

      <Row gutter={16} align="top">
        <Col xs={24} xl={14}>
          <Card
            title={`关键帧（${keyframes.length} 张）`}
            extra={
              <Space>
                <Button size="small" icon={<PlusOutlined />} onClick={addKeyframeSlot}>
                  新增空位
                </Button>
                <Button
                  size="small"
                  type="primary"
                  ghost
                  icon={<PictureOutlined />}
                  onClick={() => openKeyframeModal()}
                >
                  生成 3 张
                </Button>
              </Space>
            }
          >
            {keyframes.length === 0 ? (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description="还没有关键帧，可先「生成 3 张」得到起幅 / 中段 / 落幅"
              />
            ) : (
              <div className="kf-grid">
                {keyframes.map((keyframe) => (
                  <div className={`kf-card${keyframe.is_primary ? ' kf-card-primary' : ''}`} key={keyframe.id}>
                    <div className="kf-thumb">
                      {keyframe.url ? (
                        <img src={mediaUrl(keyframe.url)} alt={keyframe.label ?? ''} loading="lazy" />
                      ) : (
                        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                          待生成
                        </Typography.Text>
                      )}
                      {keyframe.is_primary && (
                        <span className="kf-badge">
                          <StarFilled /> 主帧
                        </span>
                      )}
                    </div>
                    <div className="kf-meta">
                      <Typography.Text ellipsis style={{ fontSize: 12.5, fontWeight: 600 }}>
                        {keyframe.label || `关键帧 ${keyframe.order_index}`}
                      </Typography.Text>
                      <Space size={2}>
                        <Tooltip title={keyframe.url ? '重新生成这张' : '生成这张关键帧'}>
                          <Button
                            size="small"
                            type="text"
                            icon={<ReloadOutlined />}
                            onClick={() => openKeyframeModal(keyframe)}
                          />
                        </Tooltip>
                        <Tooltip title={keyframe.is_primary ? '已是主帧' : '设为主帧（视频默认首帧）'}>
                          <Button
                            size="small"
                            type="text"
                            disabled={keyframe.is_primary || !keyframe.media_id}
                            icon={keyframe.is_primary ? <StarFilled /> : <StarOutlined />}
                            onClick={() => setPrimary(keyframe)}
                          />
                        </Tooltip>
                        <Tooltip title="放大查看">
                          <Button
                            size="small"
                            type="text"
                            disabled={!keyframe.url}
                            icon={<FullscreenOutlined />}
                            onClick={() =>
                              keyframe.url &&
                              setPreview({
                                kind: 'image',
                                url: mediaUrl(keyframe.url) ?? keyframe.url,
                                title: `${shot.title || `镜头 ${shot.order_index}`} · ${keyframe.label ?? ''}`,
                              })
                            }
                          />
                        </Tooltip>
                        <Popconfirm
                          title="移除该关键帧？"
                          description="仅解除与该分镜的关联，图片仍保留在素材库。"
                          okText="移除"
                          cancelText="取消"
                          okButtonProps={{ danger: true }}
                          onConfirm={() => removeKeyframe(keyframe)}
                        >
                          <Button size="small" type="text" danger icon={<DeleteOutlined />} />
                        </Popconfirm>
                      </Space>
                    </div>
                    {keyframe.prompt && (
                      <Typography.Paragraph
                        type="secondary"
                        ellipsis={{ rows: 2 }}
                        style={{ fontSize: 11.5, marginBottom: 0 }}
                      >
                        {keyframe.prompt}
                      </Typography.Paragraph>
                    )}
                  </div>
                ))}
              </div>
            )}
          </Card>
        </Col>

        <Col xs={24} xl={10}>
          <Card
            title="视频"
            extra={
              <Space>
                {shot.video_url && (
                  <Button
                    size="small"
                    icon={<FullscreenOutlined />}
                    onClick={() =>
                      setPreview({
                        kind: 'video',
                        url: mediaUrl(shot.video_url) ?? shot.video_url!,
                        title: shot.title || `镜头 ${shot.order_index}`,
                      })
                    }
                  >
                    放大播放
                  </Button>
                )}
                <Button size="small" type="primary" ghost icon={<VideoCameraAddOutlined />} onClick={openVideoModal}>
                  {shot.video_url ? '重新生成' : '生成视频'}
                </Button>
              </Space>
            }
          >
            {shot.video_url ? (
              <video
                className="shot-video"
                src={mediaUrl(shot.video_url)}
                controls
                preload="metadata"
              />
            ) : (
              <Empty description="还没有视频，选中主帧后点击「生成视频」" image={Empty.PRESENTED_IMAGE_SIMPLE} />
            )}
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              图生视频默认使用主关键帧
              {primaryKeyframe?.label ? `「${primaryKeyframe.label}」` : ''}
              作为首帧，也可在生成弹窗中改选。
            </Typography.Text>
          </Card>
        </Col>
      </Row>

      <Card
        title="镜头属性"
        style={{ marginTop: 16 }}
        extra={
          <Button type="primary" icon={<SaveOutlined />} loading={saving} onClick={saveShot}>
            保存
          </Button>
        }
      >
        <Form form={form} layout="vertical" requiredMark={false}>
          <Row gutter={12}>
            <Col xs={24} md={10}>
              <Form.Item name="title" label="标题">
                <Input maxLength={200} />
              </Form.Item>
            </Col>
            <Col xs={12} md={4}>
              <Form.Item name="duration_seconds" label="时长（秒）" tooltip="AI 拆解时逐镜决定：单一动作 5-6 秒，带动作走位 10-12 秒">
                <InputNumber min={1} max={30} step={0.5} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col xs={12} md={4}>
              <Form.Item name="resolution" label="分辨率" tooltip="AI 按本镜重要程度选择：关键情绪与特写 1080p，过场与远景 720p">
                <Select allowClear options={RESOLUTIONS.map((r) => ({ value: r, label: r }))} placeholder="跟随模型" />
              </Form.Item>
            </Col>
            <Col xs={24} md={6}>
              <Form.Item name="camera" label="运镜备注">
                <Input placeholder="仅作备注；参与生成请用下方「镜头语言（分层）」" maxLength={200} />
              </Form.Item>
            </Col>
          </Row>
          <Form.Item
            name="description"
            label={<FieldLabel text="画面描述" busy={aiBusy === 'description'} onClick={() => void generateField('description')} />}
            extra={
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                只写动作、神态与情绪；环境、镜头语言、外貌服装、风格画质都由分层注入，正文不要重复。
              </Typography.Text>
            }
          >
            <Input.TextArea autoSize={{ minRows: 2, maxRows: 5 }} placeholder="这个镜头里发生了什么、人物动作与情绪" />
          </Form.Item>
          <Form.Item name="dialogue" label="对白 / 旁白">
            <Input.TextArea autoSize={{ minRows: 2, maxRows: 4 }} />
          </Form.Item>
          <Row gutter={12}>
            <Col xs={24} md={12}>
              <Form.Item
                name="image_prompt"
                label={<FieldLabel text="关键帧提示词" busy={aiBusy === 'image_prompt'} onClick={() => void generateField('image_prompt')} />}
                extra={
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    正文 {String(imagePromptValue ?? '').length} 字；只写动作瞬间 / 神态 / 道具状态 / 材质，其余由分层注入
                  </Typography.Text>
                }
              >
                <Input.TextArea autoSize={{ minRows: 2, maxRows: 5 }} placeholder="只写动作瞬间、神态、道具状态与材质细节；环境 / 镜头语言 / 外貌服装 / 风格画质不要重复" />
              </Form.Item>
            </Col>
            <Col xs={24} md={12}>
              <Form.Item
                name="video_prompt"
                label={<FieldLabel text="视频提示词" busy={aiBusy === 'video_prompt'} onClick={() => void generateField('video_prompt')} />}
                extra={
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    正文 {String(videoPromptValue ?? '').length} 字；只写动作变化，时间轴 / 环境 / 镜头语言 / 画质由分层注入
                  </Typography.Text>
                }
              >
                <Input.TextArea autoSize={{ minRows: 2, maxRows: 5 }} placeholder="只写动作随时间的变化（起始 → 关键变化 → 收尾）；时间轴、环境、镜头语言不要重复" />
              </Form.Item>
            </Col>
          </Row>
        </Form>

        {neighbors.length > 0 && (
          <div style={{ marginTop: 12, paddingTop: 12, borderTop: '1px solid rgba(0,0,0,0.06)' }}>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              前后镜头衔接（生成关键帧 / 视频时会自动把这部分上下文拼进提示词，保持画面连续）
            </Typography.Text>
            <div style={{ marginTop: 8, display: 'flex', flexDirection: 'column', gap: 6 }}>
              {neighbors.map((item) => (
                <div key={item.key} style={{ fontSize: 12.5 }}>
                  <Tag style={{ marginRight: 8 }}>{item.label}</Tag>
                  <Typography.Text type="secondary">镜头 {item.order}</Typography.Text>
                  {item.title ? ` · ${item.title}` : ''}
                  {item.summary ? (
                    <Typography.Paragraph type="secondary" ellipsis={{ rows: 2 }} style={{ fontSize: 12, margin: '4px 0 0' }}>
                      {item.summary}
                    </Typography.Paragraph>
                  ) : null}
                </div>
              ))}
            </div>
          </div>
        )}

        {shot.directives && (
          <div>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              镜头指令（AI 拆解生成）
            </Typography.Text>
            <div style={{ marginTop: 8, display: 'flex', flexWrap: 'wrap', gap: 6 }}>
              {[
                shot.directives.shot_size,
                shot.directives.camera_angle,
                shot.directives.camera_movement,
                shot.directives.composition,
                shot.directives.atmosphere,
                ...(shot.directives.mood_tags ?? []),
              ]
                .filter(Boolean)
                .map((value) => (
                  <Tag key={String(value)} bordered={false} style={{ margin: 0 }}>
                    {value}
                  </Tag>
                ))}
            </div>
            {shot.directives.script_excerpt && (
              <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: 10, marginBottom: 0 }}>
                剧本片段：{shot.directives.script_excerpt}
              </Typography.Paragraph>
            )}
            {(shot.directives.action_beats ?? []).length > 0 && (
              <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginBottom: 0 }}>
                动作拍点：{(shot.directives.action_beats ?? []).join(' → ')}
              </Typography.Paragraph>
            )}
          </div>
        )}
      </Card>

      {/* 结构化镜头上下文：AI 拆解产出，可就地纠正；生成时按这里解析参考图与拍点 */}
      <Card
        title="结构化镜头上下文"
        style={{ marginTop: 16 }}
        extra={
          <Space>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              AI 拆解产出 · 可手动纠正
            </Typography.Text>
            <Button type="primary" icon={<SaveOutlined />} loading={ctxSaving} onClick={() => void saveContext()}>
              保存
            </Button>
          </Space>
        }
      >
        <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: -8 }}>
          生成时会按「出场主体」里的角色取参考图、按「道具归属」决定谁拿什么、按时间轴写视频拍点。
          改完保存后，点「生成关键帧 / 生成视频」只会重跑本镜，不需要重新拆解整集。
        </Typography.Paragraph>

        {ctxSubjects.length === 0 && ctxProps.length === 0 && ctxTimeline.length === 0 && (
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 12 }}
            message="这个镜头没有结构化上下文（多为迁移前生成的旧分镜）"
            description="没有主体绑定就无法按角色取参考图，也拿不到道具归属。可以手动补一条主体，或重新拆解。"
          />
        )}

        <Typography.Text strong>出场主体（谁 · 在哪 · 做什么 · 什么神态 · 手里拿什么）</Typography.Text>
        <div style={{ marginTop: 8 }}>
          {ctxSubjects.map((item, index) => (
            <div
              key={index}
              style={{ border: '1px solid rgba(0,0,0,0.08)', borderRadius: 8, padding: 10, marginBottom: 8 }}
            >
              <Row gutter={8} align="middle">
                <Col xs={24} md={7}>
                  <Input
                    size="small"
                    value={item.name}
                    placeholder="姓名（写名字，不要用「他」）"
                    onChange={(event) => patchSubject(index, { name: event.target.value })}
                  />
                </Col>
                <Col xs={24} md={8}>
                  <Select
                    size="small"
                    style={{ width: '100%' }}
                    allowClear
                    placeholder="绑定资产（决定用哪张参考图）"
                    value={item.asset_id ?? undefined}
                    options={actorOptions}
                    onChange={(value) => patchSubject(index, { asset_id: value ?? null })}
                  />
                </Col>
                <Col xs={24} md={8}>
                  <Input
                    size="small"
                    value={item.position ?? ''}
                    placeholder="站位与朝向（画面左侧，侧身面向右后方）"
                    onChange={(event) => patchSubject(index, { position: event.target.value })}
                  />
                </Col>
                <Col xs={24} md={1} style={{ textAlign: 'right' }}>
                  <Button
                    size="small"
                    type="text"
                    danger
                    icon={<DeleteOutlined />}
                    onClick={() => setCtxSubjects((prev) => prev.filter((_, i) => i !== index))}
                  />
                </Col>
              </Row>
              <Row gutter={8} style={{ marginTop: 8 }}>
                <Col xs={24} md={8}>
                  <Input
                    size="small"
                    value={item.action ?? ''}
                    placeholder="此刻动作"
                    onChange={(event) => patchSubject(index, { action: event.target.value })}
                  />
                </Col>
                <Col xs={12} md={6}>
                  <Input
                    size="small"
                    value={item.expression ?? ''}
                    placeholder="面部神态"
                    onChange={(event) => patchSubject(index, { expression: event.target.value })}
                  />
                </Col>
                <Col xs={12} md={5}>
                  <Input
                    size="small"
                    value={item.gaze ?? ''}
                    placeholder="视线看向谁"
                    onChange={(event) => patchSubject(index, { gaze: event.target.value })}
                  />
                </Col>
                <Col xs={24} md={5}>
                  <Input
                    size="small"
                    value={item.holds ?? ''}
                    placeholder="手里拿什么"
                    onChange={(event) => patchSubject(index, { holds: event.target.value })}
                  />
                </Col>
              </Row>
            </div>
          ))}
          <Button
            size="small"
            type="dashed"
            icon={<PlusOutlined />}
            onClick={() =>
              setCtxSubjects((prev) => [
                ...prev,
                {
                  name: '',
                  position: '',
                  action: '',
                  expression: '',
                  gaze: '',
                  holds: '',
                  is_primary: prev.length === 0,
                },
              ])
            }
          >
            添加主体
          </Button>
        </div>

        <Typography.Text strong style={{ display: 'block', marginTop: 16 }}>
          道具归属（谁拿什么，避免道具换人）
        </Typography.Text>
        <div style={{ marginTop: 8 }}>
          {ctxProps.map((item, index) => (
            <Row gutter={8} key={index} align="middle" style={{ marginBottom: 8 }}>
              <Col xs={24} md={7}>
                <Input
                  size="small"
                  value={item.name}
                  placeholder="道具名称"
                  onChange={(event) => patchProp(index, { name: event.target.value })}
                />
              </Col>
              <Col xs={24} md={6}>
                <Select
                  size="small"
                  style={{ width: '100%' }}
                  allowClear
                  placeholder="持有者"
                  value={item.owner ?? undefined}
                  options={ctxSubjects
                    .filter((subject) => subject.name.trim())
                    .map((subject) => ({ value: subject.name, label: subject.name }))}
                  onChange={(value) => patchProp(index, { owner: value ?? null })}
                />
              </Col>
              <Col xs={24} md={10}>
                <Input
                  size="small"
                  value={item.state ?? ''}
                  placeholder="当前状态（被推动 / 举在半空 / 掉落）"
                  onChange={(event) => patchProp(index, { state: event.target.value })}
                />
              </Col>
              <Col xs={24} md={1} style={{ textAlign: 'right' }}>
                <Button
                  size="small"
                  type="text"
                  danger
                  icon={<DeleteOutlined />}
                  onClick={() => setCtxProps((prev) => prev.filter((_, i) => i !== index))}
                />
              </Col>
            </Row>
          ))}
          <Button
            size="small"
            type="dashed"
            icon={<PlusOutlined />}
            onClick={() => setCtxProps((prev) => [...prev, { name: '', owner: null, state: '' }])}
          >
            添加道具
          </Button>
        </div>

        <Typography.Text strong style={{ display: 'block', marginTop: 16 }}>
          秒级动作时间轴（视频提示词按此写成带秒数的拍点）
        </Typography.Text>
        <div style={{ marginTop: 8 }}>
          {ctxTimeline.map((beat, index) => (
            <Row gutter={8} key={index} align="middle" style={{ marginBottom: 8 }}>
              <Col xs={6} md={2}>
                <InputNumber
                  size="small"
                  min={0}
                  step={0.5}
                  value={beat.start}
                  style={{ width: '100%' }}
                  onChange={(value) => patchBeat(index, { start: Number(value ?? 0) })}
                />
              </Col>
              <Col xs={6} md={2}>
                <InputNumber
                  size="small"
                  min={0}
                  step={0.5}
                  value={beat.end}
                  style={{ width: '100%' }}
                  onChange={(value) => patchBeat(index, { end: Number(value ?? 0) })}
                />
              </Col>
              <Col xs={24} md={8}>
                <Input
                  size="small"
                  value={beat.action ?? ''}
                  placeholder="这一时段的动作"
                  onChange={(event) => patchBeat(index, { action: event.target.value })}
                />
              </Col>
              <Col xs={12} md={4}>
                <Input
                  size="small"
                  value={beat.expression ?? ''}
                  placeholder="神态"
                  onChange={(event) => patchBeat(index, { expression: event.target.value })}
                />
              </Col>
              <Col xs={12} md={4}>
                <Input
                  size="small"
                  value={beat.gaze ?? ''}
                  placeholder="视线"
                  onChange={(event) => patchBeat(index, { gaze: event.target.value })}
                />
              </Col>
              <Col xs={24} md={3}>
                <Input
                  size="small"
                  value={beat.camera ?? ''}
                  placeholder="运镜"
                  onChange={(event) => patchBeat(index, { camera: event.target.value })}
                />
              </Col>
              <Col xs={24} md={1} style={{ textAlign: 'right' }}>
                <Button
                  size="small"
                  type="text"
                  danger
                  icon={<DeleteOutlined />}
                  onClick={() => setCtxTimeline((prev) => prev.filter((_, i) => i !== index))}
                />
              </Col>
            </Row>
          ))}
          <Button size="small" type="dashed" icon={<PlusOutlined />} onClick={addBeat}>
            添加拍点
          </Button>
        </div>

        <Row gutter={12} style={{ marginTop: 16 }}>
          <Col xs={24} md={12}>
            <Typography.Text strong>本镜起始状态</Typography.Text>
            <div style={{ marginTop: 8, display: 'grid', gap: 6 }}>
              <Input
                size="small"
                addonBefore="地点"
                value={ctxStart.location ?? ''}
                onChange={(event) => patchState('start', 'location', event.target.value)}
              />
              <Input
                size="small"
                addonBefore="时间"
                value={ctxStart.time_of_day ?? ''}
                placeholder="日 / 夜"
                onChange={(event) => patchState('start', 'time_of_day', event.target.value)}
              />
              <Input
                size="small"
                addonBefore="天气"
                value={ctxStart.weather ?? ''}
                onChange={(event) => patchState('start', 'weather', event.target.value)}
              />
              <Input
                size="small"
                addonBefore="光线"
                value={ctxStart.light ?? ''}
                placeholder="主光方向与冷暖"
                onChange={(event) => patchState('start', 'light', event.target.value)}
              />
              <Input
                size="small"
                addonBefore="站位"
                value={ctxStart.positions ?? ''}
                onChange={(event) => patchState('start', 'positions', event.target.value)}
              />
            </div>
          </Col>
          <Col xs={24} md={12}>
            <Typography.Text strong>本镜结束状态（下一镜会承接）</Typography.Text>
            <div style={{ marginTop: 8, display: 'grid', gap: 6 }}>
              <Input
                size="small"
                addonBefore="地点"
                value={ctxEnd.location ?? ''}
                onChange={(event) => patchState('end', 'location', event.target.value)}
              />
              <Input
                size="small"
                addonBefore="站位"
                value={ctxEnd.positions ?? ''}
                placeholder="动作结束时的位置与姿态"
                onChange={(event) => patchState('end', 'positions', event.target.value)}
              />
              <Input
                size="small"
                addonBefore="神态"
                value={ctxEnd.expression ?? ''}
                onChange={(event) => patchState('end', 'expression', event.target.value)}
              />
              <Input
                size="small"
                addonBefore="道具"
                value={ctxEnd.props ?? ''}
                placeholder="道具结束状态"
                onChange={(event) => patchState('end', 'props', event.target.value)}
              />
            </div>
          </Col>
        </Row>
        {ctxTimeline.length > 0 && (
          <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: 10, marginBottom: 0 }}>
            时间轴当前收尾在 {Math.max(...ctxTimeline.map((beat) => Number(beat.end) || 0))} 秒，镜头时长{' '}
            {shot.duration_seconds} 秒。
          </Typography.Paragraph>
        )}
      </Card>

      {/* 镜头语言分层：每层独立填写、独立生成，改一层不影响其它层 */}
      <Card
        title="镜头语言（分层）"
        style={{ marginTop: 16 }}
        extra={
          <Space>
            <Button size="small" onClick={() => void openPreview('image')}>
              预览关键帧提示词
            </Button>
            <Button size="small" onClick={() => void openPreview('video')}>
              预览视频提示词
            </Button>
            <Button
              type="primary"
              size="small"
              icon={<SaveOutlined />}
              loading={directiveSaving}
              onClick={() => void saveDirectives()}
            >
              保存
            </Button>
          </Space>
        }
      >
        <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: -8 }}>
          景别 / 机位 / 运镜 / 构图 / 氛围 / 情绪各自独立，生成提示词时按此顺序注入，并列为不可裁剪的硬约束。
          每层可单独让 AI 生成，只回填这一层。
        </Typography.Paragraph>
        <Row gutter={12}>
          {DIRECTIVE_FIELDS.map((item) => (
            <Col xs={24} md={item.md ?? 8} key={item.key}>
              <div style={{ marginBottom: 10 }}>
                <FieldLabel
                  text={item.label}
                  busy={directiveBusy === item.key}
                  onClick={() => void suggestDirective(item.key)}
                />
                {item.options ? (
                  <Select
                    style={{ width: '100%', marginTop: 4 }}
                    allowClear
                    showSearch
                    placeholder={item.placeholder}
                    value={ctxDirectives[item.key] || undefined}
                    options={item.options.map((value) => ({ value, label: value }))}
                    onChange={(value) =>
                      setCtxDirectives((prev) => ({ ...prev, [item.key]: value ?? '' }))
                    }
                  />
                ) : (
                  <Input
                    style={{ marginTop: 4 }}
                    placeholder={item.placeholder}
                    value={ctxDirectives[item.key] ?? ''}
                    onChange={(event) =>
                      setCtxDirectives((prev) => ({ ...prev, [item.key]: event.target.value }))
                    }
                  />
                )}
              </div>
            </Col>
          ))}
        </Row>
      </Card>

      {/* 拼装预览：生成前就能看到真实提示词、各层字数与是否被裁 */}
      <Modal
        title={`拼装预览 · ${previewKind === 'image' ? '关键帧提示词' : '视频提示词'}`}
        open={previewOpen}
        onCancel={() => setPreviewOpen(false)}
        footer={null}
        width="min(920px, 96vw)"
        destroyOnClose
      >
        {previewLoading ? (
          <div style={{ textAlign: 'center', padding: '40px 0' }}>
            <Spin />
          </div>
        ) : previewData ? (
          <div>
            <Space size={12} wrap style={{ marginBottom: 8 }}>
              <Typography.Text>
                共 <Typography.Text strong>{previewData.chars}</Typography.Text> 字
                {previewData.max_chars ? ` / 上限 ${previewData.max_chars}` : ''}
              </Typography.Text>
              {previewData.aspect_ratio && <Tag>{previewData.aspect_ratio}</Tag>}
              {previewData.project_title && <Tag color="blue">{previewData.project_title}</Tag>}
            </Space>
            {previewData.max_chars && (
              <Progress
                percent={Math.min(100, Math.round((previewData.chars / previewData.max_chars) * 100))}
                status={previewData.chars >= previewData.max_chars ? 'exception' : 'active'}
                showInfo={false}
                style={{ marginBottom: 4 }}
              />
            )}
            {previewData.dropped.length > 0 && (
              <Alert
                type="warning"
                showIcon
                style={{ marginBottom: 8 }}
                message={`以下分块已因字数上限被整块丢弃：${previewData.dropped.join('、')}`}
              />
            )}
            <div style={{ margin: '10px 0' }}>
              {previewData.blocks.map((block) => (
                <div key={block.tag} style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  <Typography.Text style={{ width: 130, fontSize: 12 }}>{block.label}</Typography.Text>
                  <Progress
                    percent={Math.min(
                      100,
                      Math.round((block.chars / Math.max(1, previewData.chars)) * 100),
                    )}
                    showInfo={false}
                    size="small"
                    style={{ flex: 1 }}
                  />
                  <Typography.Text type="secondary" style={{ width: 60, textAlign: 'right', fontSize: 12 }}>
                    {block.chars} 字
                  </Typography.Text>
                </div>
              ))}
            </div>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              以下是实际会发给模型的完整提示词（预览按当前字段估算，参考图以生成时实际选中为准）。
              正文只写动作层；环境、镜头语言、时间轴、风格、画质、负面与前后衔接都由分层编译注入，
              总字数已包含这些层。上一镜的结尾状态计入本镜预算，不占用上一镜的字数。
            </Typography.Text>
            <pre
              style={{
                marginTop: 6,
                maxHeight: 320,
                overflow: 'auto',
                background: 'rgba(0,0,0,0.03)',
                borderRadius: 6,
                padding: 10,
                fontSize: 12,
                whiteSpace: 'pre-wrap',
                wordBreak: 'break-word',
              }}
            >
              {previewData.prompt}
            </pre>
          </div>
        ) : (
          <Empty />
        )}
      </Modal>

      {/* 生成 / 重绘关键帧 */}
      <Modal
        title="生成关键帧"
        open={kfOpen}
        onCancel={() => setKfOpen(false)}
        onOk={submitKeyframe}
        confirmLoading={kfBusy}
        okText="开始生成"
        cancelText="取消"
        destroyOnClose
      >
        <Form form={kfForm} layout="vertical" requiredMark={false} style={{ marginTop: 12 }}>
          <Form.Item
            name="credential_id"
            label="图片模型密钥"
            rules={[{ required: true, message: '请选择图片密钥' }]}
          >
            <CredentialPicker credentials={imageCreds} preferProvider="agnes" placeholder="选择 Agnes / Magic Hour 等" />
          </Form.Item>

          <Form.Item name="style" label="画风 / 画面风格">
            <Select
              allowClear
              placeholder="可选：电影感 / 日式动漫 / 国风水墨…"
              options={STYLE_PRESETS.map((style) => ({ value: style.key, label: style.label }))}
            />
          </Form.Item>
          <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: -8 }}>
            「画风」= 整张图的观感基调：选了之后会把对应风格关键词追加到本次所有提示词末尾。
            不确定就先留空，用默认的项目风格。
          </Typography.Paragraph>
          <Form.Item noStyle shouldUpdate={(prev, next) => prev.style !== next.style}>
            {({ getFieldValue }) => {
              const picked = STYLE_PRESETS.find((style) => style.key === getFieldValue('style'))
              return picked ? (
                <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: -8 }}>
                  本次提示词将追加：{picked.keywords}
                </Typography.Paragraph>
              ) : null
            }}
          </Form.Item>

          <Form.Item
            name="asset_ids"
            label={
              <Space size={6}>
                <span>关联资产</span>
                <Tooltip title="勾选角色 / 场景 / 道具 / 服装，后端会把它们的固定外观描述拼进提示词，保证跨镜头是同一个人、同一个场景。可在「资产管理」里维护。">
                  <TeamOutlined style={{ color: '#8a93a8' }} />
                </Tooltip>
              </Space>
            }
          >
            <Select
              mode="multiple"
              allowClear
              placeholder={assets.length ? '选择本次生成要锁定的角色 / 场景 / 道具' : '该项目还没有资产，可到「资产管理」页新建'}
              options={assetOptions}
              optionFilterProp="label"
              maxTagCount="responsive"
            />
          </Form.Item>

          <Form.Item
            name="negative_prompt"
            label="负面提示词（留空使用系统内置默认值）"
            tooltip="写不想出现的元素，如：多余手指、水印、文字、模糊。留空即使用内置默认负面词表。"
          >
            <Input.TextArea autoSize={{ minRows: 1, maxRows: 3 }} placeholder="留空即可；要完全关闭可填写一个空格" />
          </Form.Item>

          <Form.Item name="batch" valuePropName="checked">
            <Checkbox>一次生成 3 张（起幅 / 中段 / 落幅），每张可单独写提示词</Checkbox>
          </Form.Item>

          <Form.Item noStyle shouldUpdate={(prev, next) => prev.batch !== next.batch}>
            {({ getFieldValue }) =>
              getFieldValue('batch') ? (
                <>
                  {KEYFRAME_PRESETS.map((preset) => (
                    <Form.Item
                      key={preset.key}
                      name={['batchPrompts', preset.key]}
                      label={
                        <Space size={8}>
                          <span>{preset.label} · 提示词</span>
                          <PromptTemplatePicker
                            scope="image"
                            context={templateContext}
                            buttonText="插入模板"
                            onInsert={(text) => {
                              const current = kfForm.getFieldValue(['batchPrompts', preset.key])
                              kfForm.setFieldValue(
                                ['batchPrompts', preset.key],
                                current && current.trim() ? `${current}\n${text}` : text,
                              )
                            }}
                          />
                        </Space>
                      }
                    >
                      <Input.TextArea autoSize={{ minRows: 2, maxRows: 4 }} placeholder={preset.hint} />
                    </Form.Item>
                  ))}
                  <div style={{ marginTop: -8, marginBottom: 12 }}>
                    <Button
                      size="small"
                      icon={<BulbOutlined />}
                      loading={batchAiBusy}
                      onClick={() => void generateBatchPrompts()}
                    >
                      用 AI 分别生成三张提示词
                    </Button>
                  </div>
                  <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: -8 }}>
                    三张各用各自的提示词；留空则回落到「分镜关键帧提示词 + 默认构图」。
                  </Typography.Paragraph>
                </>
              ) : (
                <>
                  <Form.Item name="target" label="生成到哪一张">
                    <Select
                      options={[
                        { value: 'new', label: '新增一张关键帧' },
                        ...keyframes.map((keyframe) => ({
                          value: keyframe.id,
                          label: `重绘：${keyframe.label ?? `关键帧 ${keyframe.order_index}`}${
                            keyframe.url ? '' : '（待生成）'
                          }`,
                        })),
                      ]}
                    />
                  </Form.Item>
                  <Form.Item name="label" label="标签（新增时使用）">
                    <Input placeholder="例如：起幅 / 特写 / 眼神" maxLength={100} />
                  </Form.Item>
                  <Form.Item
                    name="prompt"
                    label={
                      <Space size={8}>
                        <FieldLabel
                          text="提示词"
                          busy={aiBusy === 'kf_prompt'}
                          onClick={() =>
                            void generateInto('kf_prompt', 'image_prompt', (value) => kfForm.setFieldsValue({ prompt: value }))
                          }
                        />
                        <PromptTemplatePicker
                          scope="image"
                          context={templateContext}
                          onInsert={(text) => {
                            const current = kfForm.getFieldValue('prompt')
                            kfForm.setFieldValue('prompt', current && current.trim() ? `${current}\n${text}` : text)
                          }}
                        />
                      </Space>
                    }
                  >
                    <Input.TextArea
                      autoSize={{ minRows: 3, maxRows: 8 }}
                      placeholder="留空则使用分镜中的关键帧提示词或画面描述；不会写就点「AI 生成」"
                    />
                  </Form.Item>
                </>
              )
            }
          </Form.Item>
        </Form>
      </Modal>

      {/* 生成视频 */}
      <Modal
        title="生成视频（图生视频）"
        open={videoOpen}
        onCancel={() => setVideoOpen(false)}
        onOk={submitVideo}
        confirmLoading={videoBusy}
        okText="开始生成"
        cancelText="取消"
        destroyOnClose
      >
        <Form form={videoForm} layout="vertical" requiredMark={false} style={{ marginTop: 12 }}>
          <Form.Item
            name="credential_id"
            label="视频模型密钥"
            rules={[{ required: true, message: '请选择视频密钥' }]}
          >
            <CredentialPicker credentials={videoCreds} preferProvider="agnes" placeholder="选择 Agnes 等视频密钥" />
          </Form.Item>
          <Form.Item name="image_media_id" label="首帧（取哪张关键帧）">
            <Select
              allowClear
              placeholder="默认使用主关键帧"
              options={keyframes
                .filter((keyframe) => keyframe.media_id)
                .map((keyframe) => ({
                  value: keyframe.media_id as number,
                  label: `${keyframe.label ?? `关键帧 ${keyframe.order_index}`}${
                    keyframe.is_primary ? '（主帧）' : ''
                  }`,
                }))}
            />
          </Form.Item>
          <Form.Item
            name="prompt"
            label={
              <Space size={8}>
                <FieldLabel
                  text="提示词"
                  busy={aiBusy === 'video_modal_prompt'}
                  onClick={() =>
                    void generateInto('video_modal_prompt', 'video_prompt', (value) =>
                      videoForm.setFieldsValue({ prompt: value }),
                    )
                  }
                />
                <PromptTemplatePicker
                  scope="video"
                  context={templateContext}
                  onInsert={(text) => {
                    const current = videoForm.getFieldValue('prompt')
                    videoForm.setFieldValue('prompt', current && current.trim() ? `${current}\n${text}` : text)
                  }}
                />
              </Space>
            }
          >
            <Input.TextArea
              autoSize={{ minRows: 3, maxRows: 8 }}
              placeholder="留空则使用分镜中的视频提示词或画面描述；不会写就点「AI 生成」"
            />
          </Form.Item>
          <Form.Item
            name="asset_ids"
            label={
              <Space size={6}>
                <span>关联资产</span>
                <Tooltip title="勾选角色 / 场景 / 道具，后端会把其固定外观描述拼进提示词，保证视频里人物不换脸、场景不跳变。">
                  <TeamOutlined style={{ color: '#8a93a8' }} />
                </Tooltip>
              </Space>
            }
          >
            <Select
              mode="multiple"
              allowClear
              placeholder="选择本次生成要锁定的角色 / 场景 / 道具"
              options={assetOptions}
              optionFilterProp="label"
              maxTagCount="responsive"
            />
          </Form.Item>
          <Form.Item
            name="negative_prompt"
            label="负面提示词（留空使用系统内置默认值）"
            tooltip="写不想出现在视频里的元素，如：脸部变形、多余手指、闪烁、跳帧、水印。"
          >
            <Input.TextArea autoSize={{ minRows: 1, maxRows: 3 }} placeholder="留空即可" />
          </Form.Item>
          <Row gutter={12}>
            <Col span={12}>
              <Form.Item name="resolution" label="分辨率">
                <Select allowClear options={RESOLUTIONS.map((r) => ({ value: r, label: r }))} placeholder="默认" />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="duration" label="时长（秒）">
                <InputNumber min={1} max={12} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
          </Row>
          {keyframes.length === 0 && (
            <Typography.Text type="warning" style={{ fontSize: 12 }}>
              该分镜还没有关键帧，将按「文生视频」执行；建议先生成关键帧。
            </Typography.Text>
          )}
        </Form>
      </Modal>

      {/* 放大预览：视频可全屏播放并拖动进度 */}
      <Modal
        title={preview?.title}
        open={!!preview}
        onCancel={() => setPreview(null)}
        footer={null}
        centered
        width={preview?.kind === 'video' ? 'min(900px, 94vw)' : 'min(720px, 94vw)'}
        destroyOnClose
      >
        {preview?.kind === 'video' ? (
          <video
            src={preview.url}
            controls
            autoPlay
            style={{ width: '100%', maxHeight: '72vh', background: '#000', borderRadius: 8 }}
          />
        ) : preview ? (
          <img src={preview.url} alt={preview.title} style={{ width: '100%', borderRadius: 8 }} />
        ) : null}
      </Modal>
    </div>
  )
}
