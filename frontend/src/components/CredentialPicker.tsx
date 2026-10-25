import { useEffect } from 'react'
import { Select, Space, Tag, Typography } from 'antd'

import type { Credential } from '@/api/types'

interface Props {
  credentials: Credential[]
  value?: number | null
  onChange?: (value: number | undefined) => void
  placeholder?: string
  disabled?: boolean
  /** 自动选中的优先厂商（如 image/video 传 'agnes'）；不传则回落到标记为默认的密钥。 */
  preferProvider?: string
}

/** 按优先厂商 → 默认标记 → 第一条 的顺序挑出应当自动选中的密钥。 */
function pickDefault(credentials: Credential[], preferProvider?: string): Credential | undefined {
  if (credentials.length === 0) return undefined
  if (preferProvider) {
    const preferred = credentials.find((item) => item.provider === preferProvider)
    if (preferred) return preferred
  }
  return credentials.find((item) => item.is_default) ?? credentials[0]
}

/** 选择用于当前生成任务的密钥（已按能力过滤）；未选择时自动选中优先厂商的密钥。 */
export default function CredentialPicker({
  credentials,
  value,
  onChange,
  placeholder = '选择密钥',
  disabled,
  preferProvider,
}: Props) {
  // 首次渲染 / 密钥异步加载完成后，若用户还没选，则自动选中优先厂商的密钥（图片/视频默认 Agnes）
  useEffect(() => {
    if (value != null || credentials.length === 0) return
    const target = pickDefault(credentials, preferProvider)
    if (target) onChange?.(target.id)
  }, [credentials, value, preferProvider, onChange])

  if (credentials.length === 0) {
    return (
      <Typography.Text type="warning" style={{ fontSize: 12 }}>
        暂无可用于该能力的密钥，请先在「密钥管理」中添加
      </Typography.Text>
    )
  }

  return (
    <Select
      style={{ width: '100%' }}
      placeholder={placeholder}
      value={value ?? undefined}
      onChange={(v) => onChange?.(v)}
      disabled={disabled}
      options={credentials.map((item) => ({
        value: item.id,
        label: (
          <Space size={6}>
            <span>{item.name}</span>
            <Tag bordered={false}>{item.provider}</Tag>
            {item.is_default && <Tag color="blue">默认</Tag>}
          </Space>
        ),
      }))}
    />
  )
}
