/** 与后端 schemas 对齐的类型定义。 */

export interface User {
  id: number
  username: string
  email?: string | null
  created_at: string
}

export interface Token {
  access_token: string
  token_type: string
  user: User
}

export interface ProviderInfo {
  key: string
  label: string
  capabilities: string[]
  default_base_url: string
  models: Record<string, string[]>
  docs_url: string
  description: string
}

export interface Credential {
  id: number
  provider: string
  name: string
  base_url?: string | null
  capabilities: string
  is_default: boolean
  masked_key: string
  last_checked_at?: string | null
  last_check_ok?: boolean | null
  last_check_message?: string | null
  created_at: string
}

export interface CredentialPayload {
  provider: string
  name: string
  api_key?: string
  base_url?: string | null
  capabilities?: string[]
  is_default?: boolean
}

export interface ConnectionTestResult {
  ok: boolean
  message: string
}

export interface Project {
  id: number
  title: string
  description?: string | null
  aspect_ratio: string
  style?: string | null
  status: string
  visual_style?: string | null
  video_style?: string | null
  seed?: number | null
  style_lock: boolean
  created_at: string
  episode_count: number
  shot_count: number
  shots_pending: number
  shots_ready: number
  shots_running: number
  progress: number
}

export interface ProjectPayload {
  title: string
  description?: string | null
  aspect_ratio?: string
  style?: string | null
  status?: string
  visual_style?: string | null
  video_style?: string | null
  seed?: number | null
  style_lock?: boolean
}

export interface ProjectSuggestPayload {
  idea: string
  aspect_ratio?: string
  provider?: string | null
  credential_id?: number | null
  model?: string | null
}

export interface ProjectSuggestion {
  title: string
  description: string
  visual_style: string
  video_style: string
  style: string
  aspect_ratio: string
  tags: string[]
}

export interface Episode {
  id: number
  project_id: number
  order_index: number
  title: string
  synopsis?: string | null
  script_text?: string | null
  /** 最近一次拆解的分段规划与理由（与分镜同一次调用产出） */
  plan?: EpisodePlan | null
  status: string
  created_at: string
  shot_count: number
}

export interface EpisodePayload {
  title?: string
  synopsis?: string | null
  script_text?: string | null
  order_index?: number
  status?: string
}

export interface ShotDirectives {
  script_excerpt?: string | null
  shot_size?: string | null
  camera_angle?: string | null
  camera_movement?: string | null
  atmosphere?: string | null
  mood_tags?: string[] | null
  action_beats?: string[] | null
  composition?: string | null
}

export interface VisualReview {
  verdict: 'pass' | 'warn' | 'fail'
  checks: Record<string, 'pass' | 'warn' | 'fail' | 'unknown'>
  reasons: string[]
  prompt_patch?: string | null
  provider?: string
  model?: string
  reviewed_at?: string
  /** AI 只给建议：永远不代替用户点「认可」 */
  advisory?: boolean
  reference_sheets?: number
}

export interface ShotKeyframe {
  id: number
  shot_id: number
  media_id?: number | null
  order_index: number
  label?: string | null
  prompt?: string | null
  is_primary: boolean
  /** 用户已人工认可这一帧（批量生视频只消费已认可的帧）；重绘后自动清空 */
  approved_at?: string | null
  created_at: string
  url?: string | null
  /** 出图时的参考图体检：数量 / 是否真正送达 / 对应角色 / 缺失原因 */
  reference_count: number
  reference_used?: boolean | null
  reference_roles: { kind?: string | null; name?: string | null }[]
  reference_warning?: string | null
  reference_issues: string[]
  /** AI 视觉初审结论（只读建议） */
  visual_review?: VisualReview | null
}

export interface ShotNeighbor {
  id: number
  order_index: number
  title?: string | null
  summary?: string | null
}

/** 切点审查：上一镜尾帧 vs 本镜首帧（人 / 服装 / 场景 / 光线 / 首帧贴合）。 */
export interface ContinuityReview {
  verdict: 'pass' | 'warn' | 'fail' | string
  checks: Record<string, 'pass' | 'warn' | 'fail' | 'unknown' | string>
  reasons: string[]
  provider: string
  model: string
  from_shot_id: number
  to_shot_id: number
  from_media_id: number
  to_media_id: number
  reviewed_at: string
  /** 只给建议，不代替人工判断 */
  advisory: boolean
}

/** 镜头里的一个主体：谁、在哪、在做什么、什么神态、手里拿什么。 */
export interface ShotSubject {
  name: string
  /** 绑定的生产资产：生成时按它取参考图，保证同一个人 */
  asset_id?: number | null
  position?: string | null
  action?: string | null
  expression?: string | null
  gaze?: string | null
  /** 手里拿的道具，与 props.owner 对应，避免道具换人 */
  holds?: string | null
  appearance?: string | null
  costume?: string | null
  is_primary?: boolean
}

export interface ShotProp {
  name: string
  asset_id?: number | null
  /** 持有者姓名，必须是 subjects 里的某个人 */
  owner?: string | null
  state?: string | null
  description?: string | null
}

/** 秒级动作拍点：视频提示词按这些拍点写成带秒数的动作。 */
export interface ShotActionBeat {
  start: number
  end: number
  action?: string | null
  expression?: string | null
  gaze?: string | null
  camera?: string | null
}

export interface ShotState {
  location?: string | null
  time_of_day?: string | null
  weather?: string | null
  light?: string | null
  positions?: string | null
  [key: string]: any
}

export interface Shot {
  id: number
  episode_id: number
  order_index: number
  title?: string | null
  description?: string | null
  dialogue?: string | null
  camera?: string | null
  duration_seconds: number
  /** AI 拆解时逐镜决定的生视频分辨率（720p / 1080p） */
  resolution?: string | null
  image_prompt?: string | null
  video_prompt?: string | null
  directives?: ShotDirectives | null
  /** 结构化镜头上下文：生成时按这些事实选参考图、拼主体绑定与时间轴 */
  subjects?: ShotSubject[] | null
  props?: ShotProp[] | null
  action_timeline?: ShotActionBeat[] | null
  start_state?: ShotState | null
  end_state?: ShotState | null
  status: string
  /** 人工确认位：已确认提示词的镜头才能进入批量生图 */
  prompt_confirmed: boolean
  video_media_id?: number | null
  video_url?: string | null
  /** 与上一镜的切点审查结论（AI 建议；视频没变时复用） */
  continuity_review?: ContinuityReview | null
  keyframes: ShotKeyframe[]
  /** 相邻镜头：生成关键帧 / 视频时会自动带入上下文，保障画面连续 */
  prev_shot?: ShotNeighbor | null
  next_shot?: ShotNeighbor | null
}

/** 单字段 AI 生成：正文 + 镜头语言分层字段（景别/机位/运镜/构图/氛围/情绪） */
export type ShotFieldKey =
  | 'description'
  | 'image_prompt'
  | 'video_prompt'
  | 'shot_size'
  | 'camera_angle'
  | 'camera_movement'
  | 'composition'
  | 'atmosphere'
  | 'mood_tags'

export interface ShotFieldSuggestPayload {
  field: ShotFieldKey
  hint?: string | null
  provider?: string | null
  credential_id?: number | null
  model?: string | null
}

export interface ShotFieldSuggestion {
  field: string
  value: string
}

/** 拼装预览：最终提示词由各层编译而成，这里给出每层字数与是否被裁 */
export interface PromptPreviewBlock {
  tag: string
  label: string
  chars: number
}

export interface PromptPreview {
  kind: string
  prompt: string
  chars: number
  max_chars?: number | null
  blocks: PromptPreviewBlock[]
  dropped: string[]
  aspect_ratio?: string | null
  project_title?: string | null
}

export interface KeyframePayload {
  media_id?: number | null
  label?: string | null
  prompt?: string | null
  order_index?: number
  is_primary?: boolean
}

export interface ShotPayload {
  title?: string | null
  description?: string | null
  dialogue?: string | null
  camera?: string | null
  duration_seconds?: number
  /** 本镜生视频用的分辨率：AI 拆解时逐镜决定，可就地改 */
  resolution?: string | null
  image_prompt?: string | null
  video_prompt?: string | null
  directives?: ShotDirectives | null
  /** 结构化上下文可直接改：改完不用重跑整集拆解 */
  subjects?: ShotSubject[] | null
  props?: ShotProp[] | null
  action_timeline?: ShotActionBeat[] | null
  start_state?: ShotState | null
  end_state?: ShotState | null
  order_index?: number
  status?: string
  /** 人工确认：审阅完该镜头提示词后置 true，批量生图只认已确认的镜头 */
  prompt_confirmed?: boolean
}

export interface MediaAsset {
  id: number
  project_id?: number | null
  shot_id?: number | null
  kind: 'image' | 'video' | 'audio' | string
  source: string
  provider?: string | null
  model?: string | null
  filename: string
  url: string
  mime_type?: string | null
  size_bytes?: number | null
  width?: number | null
  height?: number | null
  duration_seconds?: number | null
  task_id?: number | null
  note?: string | null
  created_at: string
}

export interface EditClipPayload {
  media_id: number
  note?: string | null
}

export interface EditCreatePayload {
  clips: EditClipPayload[]
  target_duration: number
  aspect_ratio: string
  pace: 'fast' | 'medium' | 'slow'
  instruction?: string | null
}

export interface EditResult {
  media_id: number
  url: string
  duration_seconds: number
  title?: string | null
  segments: number
  aspect_ratio: string
}

export type TaskKind = 'text' | 'image' | 'video' | 'export' | 'edit'
export type TaskStatus = 'pending' | 'running' | 'succeeded' | 'failed' | 'canceled'

export interface GenerationTask {
  id: number
  project_id?: number | null
  episode_id?: number | null
  shot_id?: number | null
  kind: TaskKind | string
  provider?: string | null
  model?: string | null
  status: TaskStatus | string
  progress: number
  params?: Record<string, unknown> | null
  result?: Record<string, any> | null
  error?: string | null
  created_at: string
  started_at?: string | null
  finished_at?: string | null
}

export interface GenerationPayload {
  provider?: string | null
  credential_id?: number | null
  model?: string | null
  prompt?: string | null
  size?: string | null
  resolution?: string | null
  aspect_ratio?: string | null
  n?: number
  duration?: number | null
  image_media_id?: number | null
  keyframe_id?: number | null
  keyframe_label?: string | null
  /** 追加一张并承接上一张，不替换该镜头当前画面（逐镜确认的主路径） */
  append?: boolean
  /** 逐张关键帧提示词：n>1 时第 i 张用 keyframe_prompts[i]（起幅/中段/落幅各用各的） */
  keyframe_prompts?: string[] | null
  /** 关联资产：后端把资产外观描述拼进提示词，保证跨镜头是同一个人 / 场景 / 道具 */
  asset_ids?: number[] | null
  /** 负面提示词：传空串表示不追加，不传用系统内置默认值 */
  negative_prompt?: string | null
  /** 提示词模板：用模板渲染后的内容作为主体提示词 */
  template_id?: number | null
  template_variables?: Record<string, string> | null
}

// ------------------------------ 生产资产 ------------------------------
export type AssetKind = 'actor' | 'scene' | 'prop' | 'costume'

export interface ProductionAsset {
  id: number
  project_id?: number | null
  kind: AssetKind | string
  name: string
  description?: string | null
  /** 稳定的外观描述：跨镜头一字不改地复用，保证一致性 */
  prompt?: string | null
  tags?: string[] | null
  attributes?: Record<string, any> | null
  media_id?: number | null
  source: string
  is_global: boolean
  created_at: string
  media_url?: string | null
  project_title?: string | null
}

export interface AssetPayload {
  kind: AssetKind | string
  name: string
  description?: string | null
  prompt?: string | null
  tags?: string[] | null
  attributes?: Record<string, any> | null
  media_id?: number | null
  project_id?: number | null
  is_global?: boolean
}

export interface AssetGeneratePayload {
  prompt?: string | null
  credential_id?: number | null
  provider?: string | null
  model?: string | null
  negative_prompt?: string | null
  n?: number
  aspect_ratio?: string | null
  size?: string | null
  project_id?: number | null
  /** 基于资产已有参考图修改：保五官，只改服装造型 */
  use_reference?: boolean
  reference_media_id?: number | null
}

// ------------------------------ 提示词模板 ------------------------------
export interface PromptTemplate {
  id: number
  category: string
  name: string
  preview?: string | null
  content: string
  variables?: string[] | null
  is_system: boolean
  is_default: boolean
  created_at: string
  category_label?: string | null
}

export interface TemplateCategoryGroup {
  key: string
  label: string
  categories: { key: string; label: string; description?: string }[]
}

export interface PromptTemplatePayload {
  category: string
  name: string
  preview?: string | null
  content: string
  is_default?: boolean
}

// ------------------------------ 制作流水线 ------------------------------
export type PipelineStatus =
  | 'pending'
  | 'running'
  | 'reviewing'
  | 'generating'
  | 'completed'
  | 'failed'

export interface PipelineCharacter {
  name: string
  age?: string | null
  role?: string | null
  appearance?: string | null
  costume?: string | null
  traits?: string | null
  tags: string[]
  asset_id?: number | null
}

export interface PipelineRun {
  id: number
  project_id?: number | null
  episode_id?: number | null
  status: PipelineStatus | string
  stage: string
  params?: Record<string, any> | null
  characters?: PipelineCharacter[] | null
  error?: string | null
  task_id?: number | null
  created_at: string
  updated_at: string
  total_shots: number
  confirmed_shots: number
  generated_shots: number
  generated_videos: number
}

export interface PipelineStartPayload {
  provider?: string | null
  credential_id?: number | null
  model?: string | null
  shot_count?: number
  style?: string | null
  skills?: string[] | null
  rebuild_characters?: boolean
}

export interface PipelineConfirmPayload {
  shot_ids?: number[] | null
  credential_id?: number | null
  provider?: string | null
  model?: string | null
  asset_ids?: number[] | null
  negative_prompt?: string | null
  include_character_assets?: boolean
}

export interface PipelineVideoPayload {
  /** 不传 = 只补齐已认可关键帧且还没有视频的镜头 */
  shot_ids?: number[] | null
  credential_id?: number | null
  provider?: string | null
  model?: string | null
  asset_ids?: number[] | null
  negative_prompt?: string | null
}

export interface BreakdownPayload {
  provider?: string | null
  credential_id?: number | null
  model?: string | null
  /** 留空由 AI 先规划再决定镜头数 */
  shot_count?: number | null
  target_seconds?: number | null
  style?: string | null
  /** 「先规划」阶段用户确认过的方案：带上后拆解必须按它展开 */
  plan?: ShotPlan | null
  /** 拆解完成后自动补角色定妆照与各镜头关键帧 */
  auto_images?: boolean
}

/** 拆解规划里的一个叙事段落 */
export interface PlanBeat {
  name: string
  shots: number
  seconds: number
  summary?: string | null
}

/** 拆解时 AI 给出的分段规划与理由 */
export interface EpisodePlan {
  reasoning: string
  beats: PlanBeat[]
  shot_count: number
  /** 这份方案是用户在拆解前确认过的 */
  confirmed?: boolean
}

/** 「先规划」阶段 AI 给出的拆解建议：用户确认后才发起真正的拆解 */
export interface ShotPlan {
  reasoning: string
  shot_count: number
  /** 上一集已落库的方案里没有这两项，展示时按缺省处理 */
  shot_seconds?: number
  target_seconds?: number
  beats: PlanBeat[]
  confirmed?: boolean
}

export interface ShotPlanPayload {
  provider?: string | null
  credential_id?: number | null
  model?: string | null
  shot_count?: number | null
  target_seconds?: number | null
  style?: string | null
}

export interface ForgePayload {
  provider?: string | null
  credential_id?: number | null
  model?: string | null
  premise: string
  style?: string | null
  shot_count?: number
  target_seconds?: number
  extra?: string | null
}
