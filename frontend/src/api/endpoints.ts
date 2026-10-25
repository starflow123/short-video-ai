import { http } from './client'
import type {
  AssetGeneratePayload,
  AssetPayload,
  BreakdownPayload,
  ConnectionTestResult,
  Credential,
  CredentialPayload,
  EditCreatePayload,
  Episode,
  EpisodePayload,
  ForgePayload,
  GenerationPayload,
  GenerationTask,
  KeyframePayload,
  MediaAsset,
  PipelineConfirmPayload,
  PipelineRun,
  PipelineStartPayload,
  Project,
  ProjectPayload,
  ProjectSuggestion,
  ProjectSuggestPayload,
  PromptTemplate,
  PromptTemplatePayload,
  ProductionAsset,
  PromptPreview,
  ProviderInfo,
  Shot,
  ShotFieldSuggestPayload,
  ShotFieldSuggestion,
  ShotKeyframe,
  ShotPayload,
  ShotPlan,
  ShotPlanPayload,
  TemplateCategoryGroup,
  Token,
  User,
} from './types'

// ------------------------------ 认证 ------------------------------
export const authApi = {
  register: (data: { username: string; password: string; email?: string; invite_code?: string }) =>
    http.post<Token>('/auth/register', data).then((r) => r.data),
  login: (data: { username: string; password: string }) =>
    http.post<Token>('/auth/login', data).then((r) => r.data),
  logout: () => http.post('/auth/logout').then((r) => r.data),
  me: () => http.get<User>('/auth/me').then((r) => r.data),
}

// ------------------------------ 厂商 / 密钥 ------------------------------
export const providerApi = {
  list: () => http.get<ProviderInfo[]>('/providers').then((r) => r.data),
}

export const credentialApi = {
  list: () => http.get<Credential[]>('/credentials').then((r) => r.data),
  create: (data: CredentialPayload) =>
    http.post<Credential>('/credentials', data).then((r) => r.data),
  update: (id: number, data: Partial<CredentialPayload>) =>
    http.patch<Credential>(`/credentials/${id}`, data).then((r) => r.data),
  remove: (id: number) => http.delete(`/credentials/${id}`).then((r) => r.data),
  test: (id: number) =>
    http.post<ConnectionTestResult>(`/credentials/${id}/test`).then((r) => r.data),
}

// ------------------------------ 项目 ------------------------------
export const projectApi = {
  list: () => http.get<Project[]>('/projects').then((r) => r.data),
  create: (data: ProjectPayload) => http.post<Project>('/projects', data).then((r) => r.data),
  suggest: (data: ProjectSuggestPayload) =>
    http.post<ProjectSuggestion>('/projects/suggest', data).then((r) => r.data),
  get: (id: number) => http.get<Project>(`/projects/${id}`).then((r) => r.data),
  update: (id: number, data: ProjectPayload) =>
    http.patch<Project>(`/projects/${id}`, data).then((r) => r.data),
  remove: (id: number) => http.delete(`/projects/${id}`).then((r) => r.data),
  listEpisodes: (id: number) =>
    http.get<Episode[]>(`/projects/${id}/episodes`).then((r) => r.data),
  createEpisode: (id: number, data: EpisodePayload) =>
    http.post<Episode>(`/projects/${id}/episodes`, data).then((r) => r.data),
  forge: (id: number, data: ForgePayload) =>
    http.post<GenerationTask>(`/projects/${id}/forge`, data).then((r) => r.data),
  export: (id: number) =>
    http.post<GenerationTask>(`/projects/${id}/export`).then((r) => r.data),
}

// ------------------------------ 分集 ------------------------------
export const episodeApi = {
  get: (id: number) => http.get<Episode>(`/episodes/${id}`).then((r) => r.data),
  update: (id: number, data: EpisodePayload) =>
    http.patch<Episode>(`/episodes/${id}`, data).then((r) => r.data),
  remove: (id: number) => http.delete(`/episodes/${id}`).then((r) => r.data),
  listShots: (id: number) => http.get<Shot[]>(`/episodes/${id}/shots`).then((r) => r.data),
  createShot: (id: number, data: ShotPayload) =>
    http.post<Shot>(`/episodes/${id}/shots`, data).then((r) => r.data),
  breakdownPlan: (id: number, data: ShotPlanPayload) =>
    http.post<ShotPlan>(`/episodes/${id}/breakdown-plan`, data).then((r) => r.data),
  breakdown: (id: number, data: BreakdownPayload) =>
    http.post<GenerationTask>(`/episodes/${id}/breakdown`, data).then((r) => r.data),
}

// ------------------------------ 分镜 ------------------------------
export const shotApi = {
  get: (id: number) => http.get<Shot>(`/shots/${id}`).then((r) => r.data),
  update: (id: number, data: ShotPayload) =>
    http.patch<Shot>(`/shots/${id}`, data).then((r) => r.data),
  remove: (id: number) => http.delete(`/shots/${id}`).then((r) => r.data),
  suggest: (id: number, data: ShotFieldSuggestPayload) =>
    http.post<ShotFieldSuggestion>(`/shots/${id}/suggest`, data).then((r) => r.data),
  /** 拼装预览：不调用模型，只返回最终提示词、各层字数与是否被裁 */
  previewPrompt: (id: number, data: { kind: 'image' | 'video'; prompt?: string }) =>
    http.post<PromptPreview>(`/shots/${id}/prompt-preview`, data).then((r) => r.data),
  generateImage: (id: number, data: GenerationPayload) =>
    http.post<GenerationTask>(`/shots/${id}/image`, data).then((r) => r.data),
  generateVideo: (id: number, data: GenerationPayload) =>
    http.post<GenerationTask>(`/shots/${id}/video`, data).then((r) => r.data),
  listKeyframes: (id: number) =>
    http.get<ShotKeyframe[]>(`/shots/${id}/keyframes`).then((r) => r.data),
  createKeyframe: (id: number, data: KeyframePayload) =>
    http.post<ShotKeyframe>(`/shots/${id}/keyframes`, data).then((r) => r.data),
  updateKeyframe: (id: number, keyframeId: number, data: KeyframePayload) =>
    http.patch<ShotKeyframe>(`/shots/${id}/keyframes/${keyframeId}`, data).then((r) => r.data),
  removeKeyframe: (id: number, keyframeId: number) =>
    http.delete(`/shots/${id}/keyframes/${keyframeId}`).then((r) => r.data),
}

// ------------------------------ 任务 ------------------------------
export const taskApi = {
  list: (params?: {
    project_id?: number
    episode_id?: number
    shot_id?: number
    kind?: string
    status?: string
    orphan?: boolean
    limit?: number
  }) => http.get<GenerationTask[]>('/tasks', { params }).then((r) => r.data),
  get: (id: number) => http.get<GenerationTask>(`/tasks/${id}`).then((r) => r.data),
  cancel: (id: number) => http.post<GenerationTask>(`/tasks/${id}/cancel`).then((r) => r.data),
  /** 批量取消进行中的任务：按项目 / 资产 / 未关联项目整批停止 */
  cancelActive: (params: { project_id?: number; asset_id?: number; orphan?: boolean; kind?: string }) =>
    http.post<{ canceled: number; task_ids: number[] }>('/tasks/cancel-active', null, { params }).then((r) => r.data),
  retry: (id: number) => http.post<GenerationTask>(`/tasks/${id}/retry`).then((r) => r.data),
  remove: (id: number) => http.delete(`/tasks/${id}`).then((r) => r.data),
  /** 批量删除任务记录：仅删已结束的，进行中的会被跳过并回传 id */
  removeBatch: (taskIds: number[]) =>
    http
      .post<{ deleted: number; skipped_ids: number[] }>('/tasks/delete-batch', { task_ids: taskIds })
      .then((r) => r.data),
  /** 按范围清空任务记录：不依赖当前已加载的一页，直接清库里该范围的全部已结束任务 */
  removeAll: (params: { project_id?: number; orphan?: boolean; kind?: string; status?: string }) =>
    http
      .post<{ deleted: number; skipped_ids: number[] }>('/tasks/delete-all', null, { params })
      .then((r) => r.data),
}

// ------------------------------ 媒体 ------------------------------
export const mediaApi = {
  list: (params?: { project_id?: number; shot_id?: number; kind?: string }) =>
    http.get<MediaAsset[]>('/media', { params }).then((r) => r.data),
  remove: (id: number) => http.delete(`/media/${id}`).then((r) => r.data),
}

// ------------------------------ 资产管理 ------------------------------
export const assetApi = {
  list: (params?: {
    kind?: string
    project_id?: number
    keyword?: string
    include_global?: boolean
  }) => http.get<ProductionAsset[]>('/assets', { params }).then((r) => r.data),
  create: (data: AssetPayload) =>
    http.post<ProductionAsset>('/assets', data).then((r) => r.data),
  /** 把一张已生成的图片存为资产，直接复用其参考图与提示词 */
  fromMedia: (data: AssetPayload & { media_id: number }) =>
    http.post<ProductionAsset>('/assets/from-media', data).then((r) => r.data),
  update: (id: number, data: Partial<AssetPayload>) =>
    http.patch<ProductionAsset>(`/assets/${id}`, data).then((r) => r.data),
  /** 按外观描述给资产出参考图；有参考图时可只改服装造型、保留五官 */
  generate: (id: number, data: AssetGeneratePayload) =>
    http.post<GenerationTask>(`/assets/${id}/generate`, data).then((r) => r.data),
  remove: (id: number) => http.delete(`/assets/${id}`).then((r) => r.data),
}

// ------------------------------ 智能剪辑 ------------------------------
export const editApi = {
  /** 上传一段待剪辑视频，直接进素材库 */
  upload: (file: File, onProgress?: (percent: number) => void) => {
    const form = new FormData()
    form.append('file', file)
    return http
      .post<MediaAsset>('/edits/upload', form, {
        headers: { 'Content-Type': 'multipart/form-data' },
        onUploadProgress: (event) => {
          if (event.total) onProgress?.(Math.round((event.loaded * 100) / event.total))
        },
      })
      .then((r) => r.data)
  },
  create: (data: EditCreatePayload) =>
    http.post<GenerationTask>('/edits', data).then((r) => r.data),
}

// ------------------------------ 提示词模板 ------------------------------
export const templateApi = {
  categories: () => http.get<TemplateCategoryGroup[]>('/prompt-templates/categories').then((r) => r.data),
  list: (params?: { category?: string; q?: string }) =>
    http.get<PromptTemplate[]>('/prompt-templates', { params }).then((r) => r.data),
  create: (data: PromptTemplatePayload) =>
    http.post<PromptTemplate>('/prompt-templates', data).then((r) => r.data),
  update: (id: number, data: Partial<PromptTemplatePayload>) =>
    http.patch<PromptTemplate>(`/prompt-templates/${id}`, data).then((r) => r.data),
  remove: (id: number) => http.delete(`/prompt-templates/${id}`).then((r) => r.data),
  render: (id: number, variables: Record<string, string>) =>
    http
      .post<{ content: string; variables: string[] }>(`/prompt-templates/${id}/render`, { variables })
      .then((r) => r.data),
}

// ------------------------------ 制作流水线 ------------------------------
export const pipelineApi = {
  latest: (episodeId: number) =>
    http.get<PipelineRun | null>(`/pipeline/episode/${episodeId}`).then((r) => r.data),
  get: (runId: number) => http.get<PipelineRun>(`/pipeline/runs/${runId}`).then((r) => r.data),
  /** 启动：建角色档案 → AI 出分镜（出完暂停等人工确认） */
  start: (episodeId: number, data: PipelineStartPayload) =>
    http.post<PipelineRun>(`/pipeline/episode/${episodeId}/start`, data).then((r) => r.data),
  /** 确认提示词后批量生图；传 shot_ids 即只重跑这些镜头 */
  confirm: (runId: number, data: PipelineConfirmPayload) =>
    http.post<PipelineRun>(`/pipeline/runs/${runId}/confirm`, data).then((r) => r.data),
}
