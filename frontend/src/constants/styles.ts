/** 常用画面风格预设：中文名 + 追加到提示词的风格关键词。 */
export interface StylePreset {
  key: string
  label: string
  keywords: string
}

export const STYLE_PRESETS: StylePreset[] = [
  { key: 'cinematic', label: '电影感', keywords: '电影级光影，浅景深，冷暖对比光，轻微胶片颗粒，高动态范围' },
  { key: 'realistic', label: '写实摄影', keywords: '真实摄影质感，自然光，细节锐利，35mm 镜头，真实皮肤纹理' },
  { key: 'anime', label: '日式动漫', keywords: '日式动画风格，赛璐璐上色，清晰线稿，明快配色' },
  { key: 'ink', label: '国风水墨', keywords: '中国水墨画风，留白构图，墨色晕染，宣纸质感' },
  { key: 'oil', label: '油画质感', keywords: '油画笔触，厚涂质感，浓郁色调，柔和过渡' },
  { key: 'cyberpunk', label: '赛博朋克', keywords: '赛博朋克，霓虹灯光，雨夜街道，青橙撞色，未来科技感' },
  { key: 'animation3d', label: '3D 动画', keywords: '3D 渲染，动画电影质感，柔和全局光照，干净材质' },
  { key: 'vintage', label: '复古胶片', keywords: '复古胶片色调，暖黄色偏色，轻微失焦，明显颗粒感' },
]

/** AutoComplete 选项：下拉里显示中文名，选中后填入「中文名：关键词」，方便直接当风格描述用。 */
export const STYLE_OPTIONS = STYLE_PRESETS.map((preset) => ({
  value: `${preset.label}：${preset.keywords}`,
  label: preset.label,
}))

/** 输入中文名或关键词都能过滤出对应预设。 */
export function filterStyleOption(input: string, option?: { value?: unknown; label?: unknown }) {
  const text = input.trim()
  if (!text) return true
  return (
    String(option?.label ?? '').includes(text) || String(option?.value ?? '').includes(text)
  )
}

/** 把所选画风的关键词追加到提示词末尾，未选画风时原样返回。 */
export function applyStyleKeyword(prompt: string, styleKey?: string) {
  const preset = STYLE_PRESETS.find((item) => item.key === styleKey)
  if (!preset) return prompt
  return prompt ? `${prompt}。画面风格：${preset.keywords}` : `画面风格：${preset.keywords}`
}
